# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""MCP Marketplace — tool author configuration, earnings ledger, and withdrawals.

Allows organisations to publish their custom tools into the paid MCP
marketplace, earn revenue from external callers, and withdraw accumulated
earnings to a settlement wallet.

Revenue share is stored in basis points (bps): 7000 = 70% to author.
All USDC amounts use atomic units (6 decimals): 1_000_000 = $1.00.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import sentry_sdk
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from config import get_settings

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_EIP55_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")

# ─── Models ───────────────────────────────────────────────────────────────────


class AuthorConfig(BaseModel):
    """Public representation of a tool author's marketplace configuration."""

    org_id: str
    settlement_wallet: str
    created_at: datetime
    updated_at: datetime


class AuthorEarning(BaseModel):
    """Single per-call earnings record."""

    id: str
    org_id: str
    tool_name: str
    caller_org_id: str
    amount_usdc: int
    author_share_usdc: int
    platform_share_usdc: int
    status: str  # "pending" | "settled" | "failed"
    created_at: datetime


class AuthorWithdrawal(BaseModel):
    """Withdrawal request record."""

    id: str
    org_id: str
    amount_usdc: int
    tx_hash: str
    wallet: str
    status: str  # "pending" | "settled" | "failed" | "exhausted"
    created_at: datetime
    settled_at: datetime | None = None
    # Sweep retry metadata (populated by auto-sweep worker; 0/'' for manual withdrawals)
    sweep_attempt_count: int = 0
    last_sweep_error: str = ""
    next_sweep_at: datetime | None = None


# ─── Database pool ────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_marketplace_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference.  Called during app lifespan startup."""
    global _pool
    _pool = pool
    logger.info("Marketplace DB ready")


async def close_marketplace_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Marketplace DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Marketplace DB not initialised — call init_marketplace_db() first")
    return _pool


# ─── Wallet validation ────────────────────────────────────────────────────────


def validate_eip55_address(address: str) -> str | None:
    """Validate an Ethereum address.  Returns error message or None if valid.

    Checks format (0x + 40 hex chars) and EIP-55 mixed-case checksum.
    """
    if not _EIP55_PATTERN.match(address):
        return "Invalid Ethereum address format (expected 0x + 40 hex characters)"

    # EIP-55 checksum verification — delegate to web3.py's canonical implementation
    # Note: Web3.keccak().hex() in hexbytes>=1.0 does NOT include the 0x prefix,
    # so manual nibble-walking against hex()[2:] is unreliable.  to_checksum_address
    # is the maintained, tested path and handles all edge cases correctly.
    try:
        from web3 import Web3

        if address != Web3.to_checksum_address(address.lower()):
            return "Address fails EIP-55 checksum — use checksummed format"
    except Exception:
        return "Address checksum validation failed"

    # Reject zero address
    if address == "0x" + "0" * 40:
        return "Zero address is not a valid settlement wallet"

    return None


# ─── Author config CRUD ──────────────────────────────────────────────────────


async def set_author_config(
    org_id: str,
    *,
    settlement_wallet: str,
) -> AuthorConfig:
    """Create or update the author's marketplace configuration.

    Validates the settlement wallet as a valid EIP-55 checksummed address.
    The platform always uses the fixed default revenue share (70/30 = 7000 bps).
    Raises ValueError on invalid input.
    """
    pool = _get_pool()

    # Validate wallet
    wallet_error = validate_eip55_address(settlement_wallet)
    if wallet_error is not None:
        raise ValueError(wallet_error)

    now = datetime.now(timezone.utc)

    await pool.execute(
        """
        INSERT INTO tool_author_config
            (org_id, settlement_wallet, created_at, updated_at)
        VALUES ($1, $2, $3, $3)
        ON CONFLICT (org_id) DO UPDATE
            SET settlement_wallet = EXCLUDED.settlement_wallet,
                updated_at = EXCLUDED.updated_at
        """,
        org_id,
        settlement_wallet,
        now,
    )

    return AuthorConfig(
        org_id=org_id,
        settlement_wallet=settlement_wallet,
        created_at=now,
        updated_at=now,
    )


async def get_author_config(org_id: str) -> AuthorConfig | None:
    """Return the author config for an org, or None if not configured."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT org_id, settlement_wallet, created_at, updated_at FROM tool_author_config WHERE org_id = $1",
        org_id,
    )
    if row is None:
        return None
    return AuthorConfig(**dict(row))


# ─── Earnings ledger ─────────────────────────────────────────────────────────


async def record_tool_call_earnings(
    author_org_id: str,
    tool_name: str,
    caller_org_id: str,
    total_cost_usdc: int,
) -> None:
    """Record a per-call earnings entry.  Fire-and-forget safe.

    Splits total_cost_usdc into author and platform shares based on the
    author's configured revenue_share_bps.
    """
    try:
        pool = _get_pool()

        config = await get_author_config(author_org_id)
        if config is None:
            logger.warning(
                "No author config for org_id=%s; earnings not recorded for tool=%s",
                author_org_id,
                tool_name,
            )
            return

        # Always use platform default revenue share (70/30 = 7000 bps).
        # Per-author overrides are not supported — all authors receive the same rate.
        bps = get_settings().marketplace_default_revenue_share_bps
        author_share = (total_cost_usdc * bps) // 10_000
        platform_share = total_cost_usdc - author_share

        await pool.execute(
            """
            INSERT INTO tool_author_earnings
                (id, org_id, tool_name, caller_org_id, amount_usdc,
                 author_share_usdc, platform_share_usdc, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
            """,
            str(uuid.uuid4()),
            author_org_id,
            tool_name,
            caller_org_id,
            total_cost_usdc,
            author_share,
            platform_share,
        )
    except Exception as exc:
        logger.warning("Failed to record tool earnings", exc_info=True)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("author_org_id", str(author_org_id))
            scope.set_tag("caller_org_id", str(caller_org_id))
            scope.set_tag("tool_name", str(tool_name))
            sentry_sdk.capture_exception(exc)


async def get_author_balance(org_id: str) -> int:
    """Return the total pending (unsettled) author earnings in atomic USDC."""
    pool = _get_pool()
    result = await pool.fetchval(
        "SELECT COALESCE(SUM(author_share_usdc), 0) FROM tool_author_earnings WHERE org_id = $1 AND status = 'pending'",
        org_id,
    )
    return int(result)


async def get_author_earnings_history(
    org_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
    tool_name: str | None = None,
) -> tuple[list[AuthorEarning], str | None]:
    """Return earnings history for an org, cursor-paginated by created_at DESC.

    Returns a tuple of (earnings, next_cursor) where next_cursor is an ISO
    timestamp string to pass as ``cursor`` in the next request, or ``None``
    if there are no further pages.

    If ``tool_name`` is provided, only earnings for that tool are returned.
    """
    pool = _get_pool()
    base_where = "WHERE org_id = $1"
    params: list = [org_id, limit]

    if tool_name is not None:
        base_where += " AND tool_name = $3"
        params.append(tool_name)

    if cursor is not None:
        cursor_clause = f"AND created_at < ${len(params) + 1}"
        params.append(cursor)
    else:
        cursor_clause = ""

    rows = await pool.fetch(
        f"""
        SELECT id, org_id, tool_name, caller_org_id, amount_usdc,
               author_share_usdc, platform_share_usdc, status, created_at
        FROM tool_author_earnings
        {base_where} {cursor_clause}
        ORDER BY created_at DESC
        LIMIT $2
        """,
        *params,
    )
    earnings = [AuthorEarning(**dict(r)) for r in rows]
    next_cursor = earnings[-1].created_at.isoformat() if len(earnings) == limit else None
    return earnings, next_cursor


# ─── Withdrawals ──────────────────────────────────────────────────────────────


async def request_withdrawal(org_id: str, amount_usdc: int) -> AuthorWithdrawal:
    """Request a withdrawal of accumulated earnings.

    Validates:
    - Author config exists (settlement wallet required)
    - Amount >= minimum withdrawal threshold
    - Amount <= pending balance
    - Cooldown period respected

    Returns the created withdrawal record.
    Raises ValueError on validation failure.
    """
    pool = _get_pool()
    settings = get_settings()

    if not settings.agent_wallet_enabled:
        raise ValueError("Withdrawals are unavailable: agent wallets are not enabled on this platform")

    # Author config required
    config = await get_author_config(org_id)
    if config is None:
        raise ValueError("Author config not set — register a settlement wallet first")

    # Minimum amount
    if amount_usdc < settings.marketplace_minimum_withdrawal_usdc:
        min_str = f"${settings.marketplace_minimum_withdrawal_usdc / 1_000_000:.2f}"
        raise ValueError(f"Minimum withdrawal amount is {min_str}")

    # Sufficient pending balance
    balance = await get_author_balance(org_id)
    if amount_usdc > balance:
        raise ValueError(f"Insufficient balance: requested {amount_usdc} atomic USDC but only {balance} pending")

    # Cooldown check
    last_withdrawal = await pool.fetchrow(
        """
        SELECT created_at FROM tool_author_withdrawals
        WHERE org_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        org_id,
    )
    if last_withdrawal is not None:
        elapsed = (datetime.now(timezone.utc) - last_withdrawal["created_at"]).total_seconds()
        if elapsed < settings.marketplace_withdrawal_cooldown_seconds:
            remaining = int(settings.marketplace_withdrawal_cooldown_seconds - elapsed)
            raise ValueError(f"Withdrawal cooldown: {remaining}s remaining")

    now = datetime.now(timezone.utc)
    withdrawal_id = str(uuid.uuid4())

    await pool.execute(
        """
        INSERT INTO tool_author_withdrawals (id, org_id, amount_usdc, wallet, status, created_at)
        VALUES ($1, $2, $3, $4, 'pending', $5)
        """,
        withdrawal_id,
        org_id,
        amount_usdc,
        config.settlement_wallet,
        now,
    )

    return AuthorWithdrawal(
        id=withdrawal_id,
        org_id=org_id,
        amount_usdc=amount_usdc,
        tx_hash="",
        wallet=config.settlement_wallet,
        status="pending",
        created_at=now,
        settled_at=None,
    )


async def process_withdrawal(withdrawal_id: str) -> AuthorWithdrawal:
    """Process a pending withdrawal: settle earnings + auto-transfer USDC via CDP.

    1. Marks matching pending earnings as 'settled' (oldest first, up to amount).
    2. Transfers USDC from the platform settlement wallet to the author's
       settlement wallet using CDP SDK.
    3. Records the tx_hash on the withdrawal row.

    If CDP transfer fails, earnings are reverted to 'pending' and the
    withdrawal is marked 'failed'.
    """
    pool = _get_pool()
    settings = get_settings()

    if not settings.agent_wallet_enabled:
        raise RuntimeError("Cannot process withdrawal: AGENT_WALLET_ENABLED is false — enable agent wallets first")

    row = await pool.fetchrow(
        "SELECT * FROM tool_author_withdrawals WHERE id = $1 AND status = 'pending'",
        withdrawal_id,
    )
    if row is None:
        raise ValueError("Withdrawal not found or not in 'pending' status")

    org_id = row["org_id"]
    amount = row["amount_usdc"]
    dest_wallet = row["wallet"]

    # Mark earnings as settled (oldest first, up to withdrawal amount)
    async with pool.acquire() as conn:
        async with conn.transaction():
            earnings_rows = await conn.fetch(
                """
                SELECT id, author_share_usdc FROM tool_author_earnings
                WHERE org_id = $1 AND status = 'pending'
                ORDER BY created_at ASC
                FOR UPDATE
                """,
                org_id,
            )

            remaining = amount
            settled_ids: list[str] = []
            settled_total = 0
            for er in earnings_rows:
                if remaining <= 0:
                    break
                row_share = er["author_share_usdc"]
                if row_share > remaining:
                    continue
                settled_ids.append(er["id"])
                settled_total += row_share
                remaining -= row_share

            if settled_ids:
                await conn.execute(
                    """
                    UPDATE tool_author_earnings
                    SET status = 'settled'
                    WHERE id = ANY($1::text[])
                    """,
                    settled_ids,
                )

            # Attempt CDP transfer
            tx_hash = ""
            transfer_failed = False
            transfer_error = ""
            if settled_total > 0 and settings.agent_wallet_enabled:
                try:
                    from agent_wallets import transfer_usdc, verify_usdc_transfer

                    tx_hash = await transfer_usdc(
                        from_cdp_account=settings.marketplace_settlement_cdp_account,
                        to_address=dest_wallet,
                        amount_usdc=settled_total,
                        chain_id=settings.marketplace_settlement_chain_id,
                    )
                    logger.info(
                        "process_withdrawal: transfer ok id=%s tx=%s amount=%d",
                        withdrawal_id,
                        tx_hash,
                        settled_total,
                    )

                    # Confirm the transaction was mined and not reverted.
                    try:
                        confirmed = await verify_usdc_transfer(
                            tx_hash=tx_hash,
                            chain_id=settings.marketplace_settlement_chain_id,
                            timeout_seconds=settings.marketplace_tx_confirm_timeout_seconds,
                        )
                    except ValueError:
                        # No RPC URL configured — skip verification, proceed optimistically.
                        logger.warning(
                            "process_withdrawal: TX verification skipped (BASE_RPC_URL not set) tx=%s id=%s",
                            tx_hash,
                            withdrawal_id,
                        )
                        confirmed = True

                    if not confirmed:
                        raise RuntimeError(f"Transaction {tx_hash} was reverted on-chain")

                except Exception as exc:
                    logger.error(
                        "process_withdrawal: CDP transfer failed id=%s: %s",
                        withdrawal_id,
                        exc,
                    )
                    with sentry_sdk.new_scope() as scope:
                        scope.set_tag("withdrawal_id", str(withdrawal_id))
                        scope.set_tag("rail", "cdp")
                        sentry_sdk.capture_exception(exc)
                    transfer_failed = True
                    transfer_error = str(exc)
                    # Revert earnings back to pending
                    if settled_ids:
                        await conn.execute(
                            """
                            UPDATE tool_author_earnings
                            SET status = 'pending'
                            WHERE id = ANY($1::text[])
                            """,
                            settled_ids,
                        )

            now = datetime.now(timezone.utc)
            if transfer_failed:
                await conn.execute(
                    """
                    UPDATE tool_author_withdrawals
                    SET status = 'failed', settled_at = $2, last_sweep_error = $3
                    WHERE id = $1
                    """,
                    withdrawal_id,
                    now,
                    transfer_error,
                )
                return AuthorWithdrawal(
                    id=withdrawal_id,
                    org_id=org_id,
                    amount_usdc=amount,
                    tx_hash="",
                    wallet=dest_wallet,
                    status="failed",
                    last_sweep_error=transfer_error,
                    created_at=row["created_at"],
                    settled_at=now,
                )

            await conn.execute(
                """
                UPDATE tool_author_withdrawals
                SET status = 'settled', tx_hash = $2, settled_at = $3
                WHERE id = $1
                """,
                withdrawal_id,
                tx_hash,
                now,
            )

    return AuthorWithdrawal(
        id=withdrawal_id,
        org_id=org_id,
        amount_usdc=amount,
        tx_hash=tx_hash,
        wallet=dest_wallet,
        status="settled",
        created_at=row["created_at"],
        settled_at=now,
    )


async def complete_withdrawal(withdrawal_id: str, tx_hash: str) -> None:
    """Record the on-chain transaction hash for a processed withdrawal."""
    pool = _get_pool()
    await pool.execute(
        "UPDATE tool_author_withdrawals SET tx_hash = $2 WHERE id = $1",
        withdrawal_id,
        tx_hash,
    )


async def list_pending_withdrawals(org_id: str | None = None) -> list[AuthorWithdrawal]:
    """List pending withdrawals.  If org_id is None, returns all (admin use)."""
    pool = _get_pool()
    if org_id is not None:
        rows = await pool.fetch(
            "SELECT * FROM tool_author_withdrawals WHERE org_id = $1 AND status = 'pending' ORDER BY created_at DESC",
            org_id,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM tool_author_withdrawals WHERE status = 'pending' ORDER BY created_at DESC",
        )
    return [AuthorWithdrawal(**dict(r)) for r in rows]


async def list_org_withdrawals(
    org_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> tuple[list[AuthorWithdrawal], str | None]:
    """Return all withdrawals (any status) for an org, cursor-paginated by created_at DESC."""
    pool = _get_pool()
    cursor_clause = "" if cursor is None else "AND created_at < $3"
    args: list = [org_id, limit, *([cursor] if cursor is not None else [])]
    rows = await pool.fetch(
        f"SELECT * FROM tool_author_withdrawals WHERE org_id = $1 {cursor_clause} ORDER BY created_at DESC LIMIT $2",
        *args,
    )
    withdrawals = [AuthorWithdrawal(**dict(r)) for r in rows]
    next_cursor = withdrawals[-1].created_at.isoformat() if len(withdrawals) == limit else None
    return withdrawals, next_cursor


async def reset_withdrawal(withdrawal_id: str) -> bool:
    """Reset a failed or exhausted withdrawal back to pending for re-processing.

    Clears retry metadata so the sweep worker will attempt it immediately.
    Returns True if the withdrawal was found and reset.
    """
    pool = _get_pool()
    result = await pool.execute(
        """
        UPDATE tool_author_withdrawals
        SET status = 'pending',
            settled_at = NULL,
            sweep_attempt_count = 0,
            last_sweep_error = '',
            next_sweep_at = NULL
        WHERE id = $1 AND status IN ('failed', 'exhausted')
        """,
        withdrawal_id,
    )
    return result.split()[-1] != "0"


async def list_exhausted_withdrawals(limit: int = 50) -> list[AuthorWithdrawal]:
    """Return exhausted withdrawals for admin inspection (newest first)."""
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT * FROM tool_author_withdrawals WHERE status = 'exhausted' ORDER BY created_at DESC LIMIT $1",
        limit,
    )
    return [AuthorWithdrawal(**dict(r)) for r in rows]


# ─── Marketplace catalog queries ─────────────────────────────────────────────


class MarketplaceTool(BaseModel):
    """Public representation of a tool listed in the marketplace catalog."""

    name: str
    qualified_name: str  # {org_slug}/{tool_name}
    description: str
    marketplace_description: str
    input_schema: dict[str, Any]
    cost_usdc: int
    author_org_name: str
    author_org_slug: str


_CATALOG_SORT_COLUMNS = {
    "name": "t.name ASC",
    "price_asc": "t.base_price_usdc ASC, t.name ASC",
    "price_desc": "t.base_price_usdc DESC, t.name ASC",
}

# Sentinel used to distinguish "filter to platform only" from "no filter".
_PLATFORM_SLUG = "platform"


async def get_marketplace_catalog(
    tool_overrides: dict[str, int] | None = None,
    default_tool_cost: int = 0,
    *,
    org_slug: str | None = None,
    sort: str = "name",
    limit: int = 100,
    cursor: str | None = None,
) -> list[MarketplaceTool]:
    """Return published marketplace tools with optional filtering and sorting.

    Args:
        tool_overrides: Admin price overrides keyed by tool name or qualified name.
        default_tool_cost: Fallback cost when no other price is available.
        org_slug: When set, return only tools from that org. Use ``"platform"``
            to return only Teardrop-owned platform tools.
        sort: One of ``"name"`` (default), ``"price_asc"``, ``"price_desc"``.
        limit: Maximum number of results to return (capped at 200).
        cursor: Opaque pagination token (base64 of last seen sort key).
    """
    if sort not in _CATALOG_SORT_COLUMNS:
        raise ValueError(f"Invalid sort '{sort}'. Allowed: {', '.join(_CATALOG_SORT_COLUMNS)}")

    limit = min(max(1, limit), 200)

    if tool_overrides is None:
        tool_overrides = {}

    import base64 as _b64
    import json as _json

    pool = _get_pool()
    catalog: list[MarketplaceTool] = []

    # ── Decode cursor (opaque base64 JSON of last seen values) ────────────
    # Cursor encodes {"sort_key": <value>, "name": <str>} for keyset pagination.
    cursor_sort_key: Any = None
    cursor_name: str | None = None
    if cursor:
        try:
            cursor_data = _json.loads(_b64.b64decode(cursor).decode())
            cursor_sort_key = cursor_data.get("sort_key")
            cursor_name = cursor_data.get("name")
        except Exception:
            pass  # Malformed cursor → ignore, return from start

    # ── Org-tool section (skip when requesting platform-only) ────────────
    if org_slug != _PLATFORM_SLUG:
        order_col = _CATALOG_SORT_COLUMNS[sort]

        # Build WHERE clause; org_slug filter and cursor are optional additions.
        where_clauses = ["t.publish_as_mcp = TRUE", "t.is_active = TRUE"]
        params: list[Any] = []

        if org_slug:
            params.append(org_slug)
            where_clauses.append(f"o.slug = ${len(params)}")

        # Keyset pagination: skip rows already seen based on sort order.
        if cursor_sort_key is not None and cursor_name is not None:
            if sort == "name":
                params.append(cursor_name)
                where_clauses.append(f"t.name > ${len(params)}")
            elif sort == "price_asc":
                params.append(cursor_sort_key)
                params.append(cursor_name)
                where_clauses.append(
                    f"(t.base_price_usdc > ${len(params) - 1} OR "
                    f"(t.base_price_usdc = ${len(params) - 1} AND t.name > ${len(params)}))"
                )
            elif sort == "price_desc":
                params.append(cursor_sort_key)
                params.append(cursor_name)
                where_clauses.append(
                    f"(t.base_price_usdc < ${len(params) - 1} OR "
                    f"(t.base_price_usdc = ${len(params) - 1} AND t.name > ${len(params)}))"
                )

        params.append(limit)
        where_sql = " AND ".join(where_clauses)

        rows = await pool.fetch(
            f"""
            SELECT t.name, t.description, t.marketplace_description, t.input_schema,
                   t.base_price_usdc,
                   o.name AS org_name, o.slug AS org_slug
            FROM org_tools t
            JOIN orgs o ON o.id = t.org_id
            WHERE {where_sql}
            ORDER BY {order_col}
            LIMIT ${len(params)}
            """,
            *params,
        )

        for r in rows:
            raw_schema = r["input_schema"]
            if isinstance(raw_schema, str):
                raw_schema = _json.loads(raw_schema)

            qualified = f"{r['org_slug']}/{r['name']}"
            # Price resolution: admin override (qualified) > admin override (bare) > author price > default
            author_price = r.get("base_price_usdc", 0)
            cost = tool_overrides.get(qualified, tool_overrides.get(r["name"], author_price or default_tool_cost))

            catalog.append(
                MarketplaceTool(
                    name=r["name"],
                    qualified_name=qualified,
                    description=r["description"],
                    marketplace_description=r["marketplace_description"] or r["description"],
                    input_schema=raw_schema,
                    cost_usdc=cost,
                    author_org_name=r["org_name"],
                    author_org_slug=r["org_slug"],
                )
            )

    # ── Platform tools (skip when requesting a specific org) ──────────────
    if org_slug is None or org_slug == _PLATFORM_SLUG:
        # Platform tools are always sorted by tool_name; apply limit only when
        # fetching platform-only (org_slug="platform") to respect the limit param.
        platform_limit_clause = f"LIMIT {limit}" if org_slug == _PLATFORM_SLUG else ""
        platform_rows = await pool.fetch(
            f"""
            SELECT tool_name, display_name, description, base_price_usdc
            FROM marketplace_platform_tools
            WHERE is_active = TRUE
            ORDER BY tool_name
            {platform_limit_clause}
            """
        )
        for pr in platform_rows:
            name = pr["tool_name"]
            cost = tool_overrides.get(name, pr["base_price_usdc"] or default_tool_cost)
            catalog.append(
                MarketplaceTool(
                    name=name,
                    qualified_name=f"platform/{name}",
                    description=pr["description"],
                    marketplace_description=pr["description"],
                    input_schema={},
                    cost_usdc=cost,
                    author_org_name="Teardrop",
                    author_org_slug="platform",
                )
            )

    return catalog


def _build_catalog_cursor(tool: MarketplaceTool, sort: str) -> str:
    """Build an opaque pagination cursor for the given tool and sort order."""
    import base64 as _b64
    import json as _json

    if sort == "price_asc" or sort == "price_desc":
        data = {"sort_key": tool.cost_usdc, "name": tool.name}
    else:
        data = {"sort_key": tool.name, "name": tool.name}
    return _b64.b64encode(_json.dumps(data).encode()).decode()


async def get_marketplace_tool_by_name(
    tool_name: str,
    org_slug: str,
) -> dict[str, Any] | None:
    """Look up a published tool by name and org slug.  Returns raw row dict or None.

    Both ``tool_name`` and ``org_slug`` must match to prevent cross-org tool
    name collisions from returning the wrong webhook.
    """
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT t.*, o.slug AS org_slug, o.name AS org_name
        FROM org_tools t
        JOIN orgs o ON o.id = t.org_id
        WHERE t.name = $1 AND o.slug = $2 AND t.publish_as_mcp = TRUE AND t.is_active = TRUE
        """,
        tool_name,
        org_slug,
    )
    if row is None:
        return None
    return dict(row)


# ─── Platform tool pricing cache ─────────────────────────────────────────────
# Module-level TTL cache: tool_name → (price_usdc, expiry_monotonic).
# Keeps the hot billing path from hitting Postgres on every MCP call.
_PLATFORM_TOOL_CACHE: dict[str, tuple[int, float]] = {}
_PLATFORM_TOOL_CACHE_TTL = 60.0  # seconds


def _invalidate_platform_tool_cache() -> None:
    """Drop the entire platform tool price cache (e.g. after admin price update)."""
    _PLATFORM_TOOL_CACHE.clear()


async def get_platform_tool_price(tool_name: str) -> int | None:
    """Return base_price_usdc for a platform-owned marketplace tool, or None if not found.

    Results are cached per-tool for 60 s.
    """
    now = time.monotonic()
    cached = _PLATFORM_TOOL_CACHE.get(tool_name)
    if cached is not None and now < cached[1]:
        return cached[0]

    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT base_price_usdc FROM marketplace_platform_tools WHERE tool_name = $1 AND is_active = TRUE",
        tool_name,
    )
    if row is None:
        return None
    price = int(row["base_price_usdc"])
    _PLATFORM_TOOL_CACHE[tool_name] = (price, now + _PLATFORM_TOOL_CACHE_TTL)
    return price


# ─── Marketplace subscriptions ───────────────────────────────────────────────


class MarketplaceSubscription(BaseModel):
    """A subscription linking an org to a marketplace tool."""

    id: str
    org_id: str
    qualified_tool_name: str
    is_active: bool
    subscribed_at: datetime
    subscribed_schema_hash: str | None = None


async def subscribe_to_tool(org_id: str, qualified_tool_name: str) -> MarketplaceSubscription:
    """Subscribe an org to a marketplace tool by qualified name (e.g. 'acme/weather').

    Validates the tool exists and is published.  Raises ValueError on invalid input.
    """
    pool = _get_pool()

    if "/" not in qualified_tool_name:
        raise ValueError("Tool name must be qualified: {org_slug}/{tool_name}")

    org_slug, tool_name = qualified_tool_name.split("/", 1)
    tool_row = await get_marketplace_tool_by_name(tool_name, org_slug)
    if tool_row is None:
        raise ValueError(f"Marketplace tool not found: {qualified_tool_name}")

    current_hash = tool_row.get("schema_hash") or ""
    sub_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    try:
        await pool.execute(
            """
            INSERT INTO org_marketplace_subscriptions
                (id, org_id, qualified_tool_name, is_active, subscribed_at, subscribed_schema_hash)
            VALUES ($1, $2, $3, TRUE, $4, $5)
            ON CONFLICT (org_id, qualified_tool_name) DO UPDATE
                SET is_active = TRUE, subscribed_at = EXCLUDED.subscribed_at,
                    subscribed_schema_hash = EXCLUDED.subscribed_schema_hash
            RETURNING id
            """,
            sub_id,
            org_id,
            qualified_tool_name,
            now,
            current_hash or None,
        )
    except Exception:
        raise ValueError(f"Failed to subscribe to {qualified_tool_name}")

    _invalidate_subscription_cache(org_id)
    return MarketplaceSubscription(
        id=sub_id,
        org_id=org_id,
        qualified_tool_name=qualified_tool_name,
        is_active=True,
        subscribed_at=now,
        subscribed_schema_hash=current_hash or None,
    )


async def unsubscribe_from_tool(subscription_id: str, org_id: str) -> bool:
    """Soft-delete a subscription.  Returns True if found and deactivated."""
    pool = _get_pool()
    result = await pool.execute(
        "UPDATE org_marketplace_subscriptions SET is_active = FALSE WHERE id = $1 AND org_id = $2 AND is_active = TRUE",
        subscription_id,
        org_id,
    )
    _invalidate_subscription_cache(org_id)
    return result.split()[-1] != "0"


async def get_org_subscriptions(org_id: str) -> list[MarketplaceSubscription]:
    """Return all active marketplace subscriptions for an org."""
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT id, org_id, qualified_tool_name, is_active, subscribed_at, subscribed_schema_hash"
        " FROM org_marketplace_subscriptions"
        " WHERE org_id = $1 AND is_active = TRUE"
        " ORDER BY subscribed_at",
        org_id,
    )
    return [MarketplaceSubscription(**dict(r)) for r in rows]


# ─── Subscription cache ───────────────────────────────────────────────────────
# Module-level TTL dict: org_id → (frozenset of qualified names, expiry_monotonic)
# Same pattern as _PRICING_CACHE in billing.py.  TTL matches org_tools_cache_ttl_seconds.
_SUBSCRIPTION_CACHE: dict[str, tuple[frozenset[str], float]] = {}


def _invalidate_subscription_cache(org_id: str) -> None:
    """Drop the cached subscription set for *org_id* (e.g. after subscribe/unsubscribe)."""
    _SUBSCRIPTION_CACHE.pop(org_id, None)


async def check_org_subscription(org_id: str, qualified_tool_name: str) -> bool:
    """Return True when *org_id* holds an active subscription to *qualified_tool_name*.

    Results are cached per-org for ``org_tools_cache_ttl_seconds`` (default 60 s).
    Cache is invalidated immediately on subscribe/unsubscribe so the hot path
    (subscribe then call) sees consistent state.
    """
    now = time.monotonic()
    cached = _SUBSCRIPTION_CACHE.get(org_id)
    if cached is not None and now < cached[1]:
        return qualified_tool_name in cached[0]
    subs = await get_org_subscriptions(org_id)
    names = frozenset(s.qualified_tool_name for s in subs)
    ttl = get_settings().org_tools_cache_ttl_seconds
    _SUBSCRIPTION_CACHE[org_id] = (names, now + ttl)
    return qualified_tool_name in names


async def build_subscribed_marketplace_tools(
    org_id: str,
) -> tuple[list, dict[str, Any]]:
    """Build LangChain StructuredTool wrappers for subscribed marketplace tools.

    Returns ``(tools_list, tools_by_name_dict)`` matching the signature of
    ``build_org_langchain_tools()``.
    """
    subs = await get_org_subscriptions(org_id)
    if not subs:
        return [], {}

    tools_list: list[StructuredTool] = []
    tools_by_name: dict[str, Any] = {}

    for sub in subs:
        qualified = sub.qualified_tool_name
        if "/" not in qualified:
            continue

        org_slug, tool_name = qualified.split("/", 1)
        tool_row = await get_marketplace_tool_by_name(tool_name, org_slug)
        if tool_row is None:
            logger.debug("Subscribed tool %s no longer published; skipping", qualified)
            continue

        current_hash = tool_row.get("schema_hash") or ""
        sub_hash = sub.subscribed_schema_hash or ""
        if sub_hash and current_hash and sub_hash != current_hash:
            logger.warning(
                "Schema drift detected for marketplace tool %s "
                "(subscribed=%s… current=%s…) — org_id=%s may be calling with a stale input schema",
                qualified,
                sub_hash[:8],
                current_hash[:8],
                org_id,
            )

        try:
            lc_tool = _build_marketplace_langchain_tool(tool_row, qualified)
            tools_list.append(lc_tool)
            tools_by_name[qualified] = lc_tool
        except Exception:
            logger.warning("Failed to build subscribed tool %s", qualified, exc_info=True)

    return tools_list, tools_by_name


def _build_marketplace_langchain_tool(
    tool_row: dict[str, Any],
    qualified_name: str,
) -> "StructuredTool":
    """Wrap a marketplace tool row as a LangChain StructuredTool via webhook."""
    import json as _json

    import aiohttp

    from org_tools import _build_pydantic_model, _decrypt_header
    from tools.definitions.http_fetch import async_validate_url

    raw_schema = tool_row.get("input_schema", {})
    if isinstance(raw_schema, str):
        raw_schema = _json.loads(raw_schema)

    model_name = f"MPTool_{qualified_name.replace('/', '_')}_Input"
    args_model = _build_pydantic_model(qualified_name, raw_schema, model_name=model_name)

    _url = tool_row["webhook_url"]
    _method = tool_row.get("webhook_method", "POST")
    _timeout_sec = tool_row.get("timeout_seconds", 10)
    _auth_name = tool_row.get("auth_header_name")
    _auth_enc = tool_row.get("auth_header_enc")

    async def _call(**kwargs: Any) -> dict[str, Any]:
        url_err = await async_validate_url(_url)
        if url_err:
            return {"error": f"Webhook URL blocked: {url_err}"}

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if _auth_name and _auth_enc:
            try:
                headers[_auth_name] = _decrypt_header(_auth_enc)
            except Exception:
                return {"error": "Failed to decrypt webhook auth header"}

        timeout = aiohttp.ClientTimeout(total=_timeout_sec)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if _method == "GET":
                    resp = await session.get(_url, headers=headers, params=kwargs)
                elif _method == "PUT":
                    resp = await session.put(_url, headers=headers, json=kwargs)
                else:
                    resp = await session.post(_url, headers=headers, json=kwargs)

                body = await resp.read()
                if len(body) > 512 * 1024:
                    body = body[: 512 * 1024]

                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return _json.loads(body)
                return {"text": body.decode("utf-8", errors="replace")}
        except asyncio.TimeoutError:
            return {"error": f"Webhook timed out after {_timeout_sec}s"}
        except Exception as exc:
            return {"error": f"Webhook request failed: {type(exc).__name__}"}

    return StructuredTool.from_function(
        coroutine=_call,
        name=qualified_name,
        description=tool_row.get("marketplace_description") or tool_row.get("description", ""),
        args_schema=args_model,
    )


# ─── Subscriber notification ─────────────────────────────────────────────────


async def notify_subscribers_of_deactivation(
    qualified_tool_name: str,
    reason: str,
) -> None:
    """Email all admin/owner users of orgs subscribed to a deactivated tool.

    Best-effort: SMTP failures are logged but never raised. A Redis-backed
    1-hour dedup guard prevents notification storms if a flapping tool re-trips
    the breaker after a manual re-enable.
    """
    from cache import get_redis
    from email_utils import send_tool_deactivated_email

    settings = get_settings()
    redis = get_redis()
    dedup_key = f"teardrop:notify:tool_deact:{qualified_tool_name}"

    # Dedup: skip if we've already notified about this tool in the last hour.
    if redis is not None:
        try:
            already = await redis.set(dedup_key, "1", ex=3600, nx=True)
            if not already:
                logger.debug(
                    "notify_subscribers_of_deactivation: dedup skip qualified=%s",
                    qualified_tool_name,
                )
                return
        except Exception:  # pragma: no cover — fall through on Redis error
            logger.warning("Redis dedup check failed; proceeding with notification")

    try:
        pool = _get_pool()
    except RuntimeError:
        return

    try:
        rows = await pool.fetch(
            """
            SELECT DISTINCT u.email
            FROM org_marketplace_subscriptions s
            JOIN users u ON u.org_id = s.org_id
            WHERE s.qualified_tool_name = $1
              AND s.is_active = TRUE
              AND u.is_active = TRUE
              AND u.role IN ('admin', 'owner')
            """,
            qualified_tool_name,
        )
    except Exception:
        logger.warning(
            "notify_subscribers_of_deactivation: query failed qualified=%s",
            qualified_tool_name,
            exc_info=True,
        )
        return

    if not rows:
        return

    catalog_url = settings.marketplace_catalog_url or ""
    coros = [
        send_tool_deactivated_email(
            to_email=row["email"],
            qualified_tool_name=qualified_tool_name,
            reason=reason,
            catalog_url=catalog_url,
        )
        for row in rows
    ]
    await asyncio.gather(*coros, return_exceptions=True)


async def auto_deactivate_tool_for_health(tool_id: str, qualified_tool_name: str | None = None) -> None:
    """Mark a tool ``is_active=FALSE`` after the circuit breaker trips.

    Mirrors the side-effects of ``delete_org_tool`` for a published tool:
    - Soft-deactivate the tool row.
    - Deactivate all marketplace subscriptions for the tool.
    - Invalidate per-org and marketplace caches.
    - Emit an audit event with reason="circuit_breaker_tripped".
    - Notify subscribers via email (best-effort).

    Idempotent: if the tool is already inactive, no further work is performed.
    """
    import sentry_sdk

    pool = _get_pool()

    row = await pool.fetchrow(
        "SELECT id, org_id, name, publish_as_mcp, is_active FROM org_tools WHERE id = $1",
        tool_id,
    )
    if row is None or not row["is_active"]:
        return

    org_id = row["org_id"]
    name = row["name"]

    result = await pool.execute(
        "UPDATE org_tools SET is_active = FALSE, updated_at = NOW() WHERE id = $1 AND is_active = TRUE",
        tool_id,
    )
    # If another caller deactivated concurrently, exit cleanly.
    if result.split()[-1] == "0":
        return

    # Audit event.
    from org_tools import _record_event, invalidate_marketplace_cache, invalidate_org_tools_cache

    await _record_event(
        org_id,
        tool_id,
        name,
        "failed",
        actor_id="system:circuit_breaker",
        detail={"reason": "circuit_breaker_tripped"},
    )

    await invalidate_org_tools_cache(org_id)

    if not row["publish_as_mcp"]:
        return  # Not a published tool — nothing more to do.

    await invalidate_marketplace_cache()

    # Resolve qualified name if not supplied.
    if qualified_tool_name is None:
        org_row = await pool.fetchrow("SELECT slug FROM orgs WHERE id = $1", org_id)
        if org_row is None:
            return
        qualified_tool_name = f"{org_row['slug']}/{name}"

    await pool.execute(
        "UPDATE org_marketplace_subscriptions SET is_active = FALSE WHERE qualified_tool_name = $1 AND is_active = TRUE",
        qualified_tool_name,
    )

    # Sentry event tagged so ops can find every breaker trip.
    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("tool_id", str(tool_id))
            scope.set_tag("org_id", str(org_id))
            scope.set_tag("circuit_breaker", "tripped")
            sentry_sdk.capture_message(
                f"Circuit breaker tripped: {qualified_tool_name}",
                level="warning",
            )
    except Exception:  # pragma: no cover
        logger.debug("sentry capture failed in auto_deactivate", exc_info=True)

    # Fire-and-forget notification.
    try:
        asyncio.create_task(
            notify_subscribers_of_deactivation(
                qualified_tool_name,
                "automatic — repeated webhook failures",
            )
        )
    except Exception:  # pragma: no cover
        logger.warning("Failed to schedule subscriber notification", exc_info=True)


def _sweep_withdrawal_id(org_id: str, epoch_hour: int) -> str:
    """Derive a deterministic withdrawal ID for a sweep cycle.

    Using the same org + hour produces the same UUID, so a worker restart
    within the same sweep cycle won't create a duplicate withdrawal record
    (the INSERT will hit the PK conflict and be ignored).
    """
    raw = hashlib.sha256(f"sweep:{org_id}:{epoch_hour}".encode()).digest()
    # Re-format as RFC 4122 variant-1 UUID (version 5 shape)
    hex_str = raw[:16].hex()
    return f"{hex_str[:8]}-{hex_str[8:12]}-5{hex_str[13:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


def _sweep_backoff_seconds(attempt: int) -> int:
    """Exponential backoff for failed sweep attempts.

    attempt=1 → 2 min, attempt=2 → 4 min, …, capped at 24 h.
    Longer intervals than billing retry (seconds) because on-chain CDP
    transfers are expensive and rate-limited.
    """
    return min(2**attempt * 60, 86_400)


async def marketplace_sweep_once() -> int:
    """Auto-create and settle withdrawals for all qualifying orgs.

    Design decisions:
    - Deterministic withdrawal IDs prevent duplicates on worker restart.
    - Orgs with an existing pending/failed withdrawal that is still in backoff
      are skipped; the withdrawal is retried once next_sweep_at elapses.
    - On CDP failure the withdrawal is marked 'failed' and a backoff timestamp
      set; after max_retries it is marked 'exhausted' for admin review.
    - The earnings query uses a subquery to exclude orgs already being
      processed by another concurrent sweep call (rare but safe).

    Returns the number of orgs whose withdrawal was successfully settled.
    """
    pool = _get_pool()
    settings = get_settings()
    min_amount = settings.marketplace_minimum_withdrawal_usdc
    max_retries = settings.marketplace_max_sweep_retries
    now = datetime.now(timezone.utc)
    # Floor to the current sweep epoch (1-hour buckets) for deterministic IDs.
    epoch_hour = int(now.timestamp()) // 3600

    # Orgs with enough pending earnings that are not currently in backoff.
    # LEFT JOIN excludes orgs whose most recent non-settled withdrawal still
    # has next_sweep_at in the future (backoff not yet elapsed).
    rows = await pool.fetch(
        """
        SELECT e.org_id, SUM(e.author_share_usdc) AS total
        FROM tool_author_earnings e
        WHERE e.status = 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM tool_author_withdrawals w
              WHERE w.org_id = e.org_id
                AND w.status IN ('pending', 'failed')
                AND (w.next_sweep_at IS NULL OR w.next_sweep_at > NOW())
          )
        GROUP BY e.org_id
        HAVING SUM(e.author_share_usdc) >= $1
        LIMIT 50
        """,
        min_amount,
    )

    processed = 0
    for r in rows:
        org_id = r["org_id"]
        total = int(r["total"])
        withdrawal_id = _sweep_withdrawal_id(org_id, epoch_hour)
        try:
            # Idempotent insert: if this ID already exists the withdrawal was
            # created in a previous (crashed) run — just process it.
            existing = await pool.fetchrow(
                "SELECT id, status FROM tool_author_withdrawals WHERE id = $1",
                withdrawal_id,
            )
            if existing is None:
                config = await get_author_config(org_id)
                if config is None:
                    logger.warning(
                        "marketplace_sweep: org=%s has no author config, skipping",
                        org_id,
                    )
                    continue
                await pool.execute(
                    """
                    INSERT INTO tool_author_withdrawals
                        (id, org_id, amount_usdc, wallet, status, created_at,
                         sweep_attempt_count, last_sweep_error, next_sweep_at)
                    VALUES ($1, $2, $3, $4, 'pending', NOW(), 0, '', NULL)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    withdrawal_id,
                    org_id,
                    total,
                    config.settlement_wallet,
                )
            elif existing["status"] == "settled":
                # Already processed in this epoch — count as success and move on.
                processed += 1
                continue

            result = await process_withdrawal(withdrawal_id)

            if result.status == "settled":
                processed += 1
                logger.info(
                    "marketplace_sweep: settled org=%s amount=%d tx=%s",
                    org_id,
                    total,
                    result.tx_hash,
                )
            else:
                # CDP transfer failed inside process_withdrawal — apply backoff.
                row = await pool.fetchrow(
                    "SELECT sweep_attempt_count FROM tool_author_withdrawals WHERE id = $1",
                    withdrawal_id,
                )
                attempt = int(row["sweep_attempt_count"]) + 1 if row else 1
                error_detail = result.last_sweep_error or "CDP transfer failed"

                if attempt >= max_retries:
                    await pool.execute(
                        """
                        UPDATE tool_author_withdrawals
                        SET status = 'exhausted',
                            sweep_attempt_count = $2,
                            last_sweep_error = $3,
                            next_sweep_at = NULL
                        WHERE id = $1
                        """,
                        withdrawal_id,
                        attempt,
                        error_detail,
                    )
                    logger.error(
                        "marketplace_sweep: exhausted withdrawal_id=%s org=%s "
                        "amount_usdc=%d attempts=%d error=%s — manual intervention required",
                        withdrawal_id,
                        org_id,
                        total,
                        attempt,
                        error_detail,
                    )
                else:
                    backoff = _sweep_backoff_seconds(attempt)
                    await pool.execute(
                        """
                        UPDATE tool_author_withdrawals
                        SET sweep_attempt_count = $2,
                            last_sweep_error = $3,
                            next_sweep_at = NOW() + ($4 * INTERVAL '1 second')
                        WHERE id = $1
                        """,
                        withdrawal_id,
                        attempt,
                        error_detail,
                        backoff,
                    )
                    logger.warning(
                        "marketplace_sweep: failed org=%s attempt=%d/%d retry_in=%ds",
                        org_id,
                        attempt,
                        max_retries,
                        backoff,
                    )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "marketplace_sweep: unexpected error org=%s: %s",
                org_id,
                exc,
                exc_info=True,
            )

    # Warn operators if the settlement wallet is running low on USDC.
    if settings.agent_wallet_enabled:
        try:
            from agent_wallets import get_settlement_wallet_balance_usdc

            balance = await get_settlement_wallet_balance_usdc(chain_id=settings.marketplace_settlement_chain_id)
            warn_threshold = settings.marketplace_settlement_warn_threshold_usdc
            if balance < warn_threshold:
                logger.error(
                    "marketplace_sweep: settlement wallet below threshold — "
                    "balance_usdc=%d threshold_usdc=%d account=%s — top up required",
                    balance,
                    warn_threshold,
                    settings.marketplace_settlement_cdp_account,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("marketplace_sweep: could not check settlement balance: %s", exc)

    return processed


async def _marketplace_sweep_loop() -> None:
    """Background task: periodically auto-process qualifying withdrawals.

    Propagates asyncio.CancelledError so the lifespan shutdown is clean.
    """
    settings = get_settings()
    interval = settings.marketplace_sweep_interval_seconds
    logger.info("marketplace_sweep_loop: started (interval=%ds)", interval)

    # Optional Sentry cron monitor wiring (no-op when DSN unset).
    cron_monitor = None
    if getattr(settings, "sentry_dsn", ""):
        try:
            from sentry_sdk.crons import monitor as sentry_monitor

            minutes = max(1, interval // 60 or 1)
            monitor_config = {
                "schedule": {"type": "interval", "value": minutes, "unit": "minute"},
                "checkin_margin": max(2, minutes // 4 or 2),
                "max_runtime": max(2, minutes * 2),
                "failure_issue_threshold": 2,
                "recovery_threshold": 2,
            }

            def cron_monitor() -> Any:  # type: ignore[no-redef]
                return sentry_monitor(
                    monitor_slug="marketplace-sweep",
                    monitor_config=monitor_config,
                )
        except ImportError:  # pragma: no cover
            cron_monitor = None

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("marketplace_sweep_loop: cancelled, shutting down")
            raise
        cancel_exc: BaseException | None = None
        try:
            if cron_monitor is not None:
                with cron_monitor():
                    try:
                        count = await marketplace_sweep_once()
                    except asyncio.CancelledError as exc:
                        cancel_exc = exc
                        count = 0
            else:
                count = await marketplace_sweep_once()
            if count:
                logger.info("marketplace_sweep_loop: settled %d orgs", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("marketplace_sweep_loop: cycle error", exc_info=True)
        if cancel_exc is not None:
            raise cancel_exc
