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

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
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
    revenue_share_bps: int
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
    status: str  # "pending" | "settled" | "failed"
    created_at: datetime
    settled_at: datetime | None = None


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

    # EIP-55 checksum verification using Keccak-256 (not SHA3-256 — they differ in padding)
    try:
        from web3 import Web3

        addr_lower = address[2:].lower()
        # Web3.keccak returns bytes; hex() gives the lowercase hex digest without 0x prefix
        addr_hash = Web3.keccak(text=addr_lower).hex()[2:]
        for i, char in enumerate(addr_lower):
            if char in "0123456789":
                continue
            expected_upper = int(addr_hash[i], 16) >= 8
            if expected_upper and address[2 + i] != char.upper():
                return "Address fails EIP-55 checksum — use checksummed format"
            if not expected_upper and address[2 + i] != char.lower():
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
    revenue_share_bps: int | None = None,
) -> AuthorConfig:
    """Create or update the author's marketplace configuration.

    Validates the settlement wallet as a valid EIP-55 checksummed address.
    Raises ValueError on invalid input.
    """
    pool = _get_pool()
    settings = get_settings()

    # Validate wallet
    wallet_error = validate_eip55_address(settlement_wallet)
    if wallet_error is not None:
        raise ValueError(wallet_error)

    # Validate revenue share bps
    if revenue_share_bps is None:
        revenue_share_bps = settings.marketplace_default_revenue_share_bps
    if not (0 <= revenue_share_bps <= 10_000):
        raise ValueError("revenue_share_bps must be between 0 and 10000")

    now = datetime.now(timezone.utc)

    await pool.execute(
        """
        INSERT INTO tool_author_config
            (org_id, settlement_wallet, revenue_share_bps, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $4)
        ON CONFLICT (org_id) DO UPDATE
            SET settlement_wallet = EXCLUDED.settlement_wallet,
                revenue_share_bps = EXCLUDED.revenue_share_bps,
                updated_at = EXCLUDED.updated_at
        """,
        org_id,
        settlement_wallet,
        revenue_share_bps,
        now,
    )

    return AuthorConfig(
        org_id=org_id,
        settlement_wallet=settlement_wallet,
        revenue_share_bps=revenue_share_bps,
        created_at=now,
        updated_at=now,
    )


async def get_author_config(org_id: str) -> AuthorConfig | None:
    """Return the author config for an org, or None if not configured."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT org_id, settlement_wallet, revenue_share_bps, created_at, updated_at"
        " FROM tool_author_config WHERE org_id = $1",
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

        bps = config.revenue_share_bps
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
    except Exception:
        logger.warning("Failed to record tool earnings", exc_info=True)


async def get_author_balance(org_id: str) -> int:
    """Return the total pending (unsettled) author earnings in atomic USDC."""
    pool = _get_pool()
    result = await pool.fetchval(
        "SELECT COALESCE(SUM(author_share_usdc), 0)"
        " FROM tool_author_earnings WHERE org_id = $1 AND status = 'pending'",
        org_id,
    )
    return int(result)


async def get_author_earnings_history(
    org_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> tuple[list[AuthorEarning], str | None]:
    """Return earnings history for an org, cursor-paginated by created_at DESC.

    Returns a tuple of (earnings, next_cursor) where next_cursor is an ISO
    timestamp string to pass as ``cursor`` in the next request, or ``None``
    if there are no further pages.
    """
    pool = _get_pool()
    if cursor is None:
        rows = await pool.fetch(
            """
            SELECT id, org_id, tool_name, caller_org_id, amount_usdc,
                   author_share_usdc, platform_share_usdc, status, created_at
            FROM tool_author_earnings
            WHERE org_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, org_id, tool_name, caller_org_id, amount_usdc,
                   author_share_usdc, platform_share_usdc, status, created_at
            FROM tool_author_earnings
            WHERE org_id = $1 AND created_at < $3
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id,
            limit,
            cursor,
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
        raise ValueError(
            f"Insufficient balance: requested {amount_usdc} atomic USDC "
            f"but only {balance} pending"
        )

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
    """Process a pending withdrawal (admin-triggered).

    Marks matching pending earnings as 'settled' up to the withdrawal amount
    and updates the withdrawal status.

    On-chain USDC transfer is NOT implemented in Phase 1 — this function
    handles the ledger bookkeeping only.  The admin should execute the
    on-chain transfer manually and provide the tx_hash via
    complete_withdrawal().
    """
    pool = _get_pool()

    row = await pool.fetchrow(
        "SELECT * FROM tool_author_withdrawals WHERE id = $1 AND status = 'pending'",
        withdrawal_id,
    )
    if row is None:
        raise ValueError("Withdrawal not found or not in 'pending' status")

    org_id = row["org_id"]
    amount = row["amount_usdc"]

    # Mark earnings as settled (oldest first, up to withdrawal amount)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock and fetch pending earnings for this org
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
            for er in earnings_rows:
                if remaining <= 0:
                    break
                row_share = er["author_share_usdc"]
                # Skip rows that exceed remaining balance — continue to find smaller rows that fit.
                # This is "best-effort" settlement: settle as much as we can without overshooting.
                # Example: withdrawal of $0.50 against rows [$0.30, $0.60, $0.10] settles $0.30 + $0.10 = $0.40.
                if row_share > remaining:
                    continue
                settled_ids.append(er["id"])
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

            # Mark the withdrawal as settled
            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE tool_author_withdrawals
                SET status = 'settled', settled_at = $2
                WHERE id = $1
                """,
                withdrawal_id,
                now,
            )

    return AuthorWithdrawal(
        id=withdrawal_id,
        org_id=org_id,
        amount_usdc=amount,
        tx_hash="",
        wallet=row["wallet"],
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
            "SELECT * FROM tool_author_withdrawals WHERE org_id = $1 AND status = 'pending'"
            " ORDER BY created_at DESC",
            org_id,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM tool_author_withdrawals WHERE status = 'pending'"
            " ORDER BY created_at DESC",
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


async def get_marketplace_catalog(
    tool_overrides: dict[str, int] | None = None,
    default_tool_cost: int = 0,
) -> list[MarketplaceTool]:
    """Return all published marketplace tools with pricing and author info.

    Combines data from org_tools, orgs, and tool_pricing_overrides.
    """
    pool = _get_pool()

    rows = await pool.fetch(
        """
        SELECT t.name, t.description, t.marketplace_description, t.input_schema,
               o.name AS org_name, o.slug AS org_slug
        FROM org_tools t
        JOIN orgs o ON o.id = t.org_id
        WHERE t.publish_as_mcp = TRUE AND t.is_active = TRUE
        ORDER BY t.name
        """
    )

    if tool_overrides is None:
        tool_overrides = {}

    import json as _json

    catalog: list[MarketplaceTool] = []
    for r in rows:
        raw_schema = r["input_schema"]
        if isinstance(raw_schema, str):
            raw_schema = _json.loads(raw_schema)

        qualified = f"{r['org_slug']}/{r['name']}"
        cost = tool_overrides.get(r["name"], default_tool_cost)

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
    return catalog


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
