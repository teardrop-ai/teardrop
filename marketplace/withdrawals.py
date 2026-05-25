# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace withdrawal lifecycle and deactivation side-effects."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import sentry_sdk

from marketplace.catalog import get_author_config
from marketplace.context import _get_pool
from marketplace.earnings import get_author_balance
from marketplace.models import AuthorWithdrawal
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


class _WithdrawalService:
    """Encapsulates withdrawal validation and settlement side-effects."""

    def __init__(self, pool: Any, settings: Any):
        self._pool = pool
        self._settings = settings

    async def request(self, org_id: str, amount_usdc: int) -> AuthorWithdrawal:
        """Create a validated pending withdrawal request."""
        if not self._settings.agent_wallet_enabled:
            raise ValueError("Withdrawals are unavailable: agent wallets are not enabled on this platform")

        config = await get_author_config(org_id)
        if config is None:
            raise ValueError("Author config not set — register a settlement wallet first")

        if amount_usdc < self._settings.marketplace_minimum_withdrawal_usdc:
            min_str = f"${self._settings.marketplace_minimum_withdrawal_usdc / 1_000_000:.2f}"
            raise ValueError(f"Minimum withdrawal amount is {min_str}")

        balance = await get_author_balance(org_id)
        if amount_usdc > balance:
            raise ValueError(f"Insufficient balance: requested {amount_usdc} atomic USDC but only {balance} pending")

        last_withdrawal = await self._pool.fetchrow(
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
            if elapsed < self._settings.marketplace_withdrawal_cooldown_seconds:
                remaining = int(self._settings.marketplace_withdrawal_cooldown_seconds - elapsed)
                raise ValueError(f"Withdrawal cooldown: {remaining}s remaining")

        now = datetime.now(timezone.utc)
        withdrawal_id = str(__import__("uuid").uuid4())
        await self._pool.execute(
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

    async def _load_pending_withdrawal(self, withdrawal_id: str):
        row = await self._pool.fetchrow(
            "SELECT * FROM tool_author_withdrawals WHERE id = $1 AND status = 'pending'",
            withdrawal_id,
        )
        if row is None:
            raise ValueError("Withdrawal not found or not in 'pending' status")
        return row

    async def _select_earnings_to_settle(
        self,
        conn,
        *,
        org_id: str,
        amount_usdc: int,
    ) -> tuple[list[str], int]:
        earnings_rows = await conn.fetch(
            """
            SELECT id, author_share_usdc FROM tool_author_earnings
            WHERE org_id = $1 AND status = 'pending'
            ORDER BY created_at ASC
            FOR UPDATE
            """,
            org_id,
        )

        remaining = amount_usdc
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
        return settled_ids, settled_total

    async def _attempt_transfer(
        self,
        *,
        withdrawal_id: str,
        dest_wallet: str,
        settled_total: int,
    ) -> tuple[str, bool, str]:
        if settled_total <= 0 or not self._settings.agent_wallet_enabled:
            return "", False, ""

        try:
            from teardrop.agent_wallets import transfer_usdc, verify_usdc_transfer

            tx_hash = await transfer_usdc(
                from_cdp_account=self._settings.marketplace_settlement_cdp_account,
                to_address=dest_wallet,
                amount_usdc=settled_total,
                chain_id=self._settings.marketplace_settlement_chain_id,
            )
            logger.info(
                "process_withdrawal: transfer ok id=%s tx=%s amount=%d",
                withdrawal_id,
                tx_hash,
                settled_total,
            )

            try:
                confirmed = await verify_usdc_transfer(
                    tx_hash=tx_hash,
                    chain_id=self._settings.marketplace_settlement_chain_id,
                    timeout_seconds=self._settings.marketplace_tx_confirm_timeout_seconds,
                )
            except ValueError:
                logger.warning(
                    "process_withdrawal: TX verification skipped (BASE_RPC_URL not set) tx=%s id=%s",
                    tx_hash,
                    withdrawal_id,
                )
                confirmed = True

            if not confirmed:
                raise RuntimeError(f"Transaction {tx_hash} was reverted on-chain")
            return tx_hash, False, ""
        except Exception as exc:
            logger.error("process_withdrawal: CDP transfer failed id=%s: %s", withdrawal_id, exc)
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("withdrawal_id", str(withdrawal_id))
                scope.set_tag("rail", "cdp")
                sentry_sdk.capture_exception(exc)
            return "", True, str(exc)

    async def process(self, withdrawal_id: str) -> AuthorWithdrawal:
        """Process a pending withdrawal and return the resulting state."""
        if not self._settings.agent_wallet_enabled:
            raise RuntimeError("Cannot process withdrawal: AGENT_WALLET_ENABLED is false — enable agent wallets first")

        row = await self._load_pending_withdrawal(withdrawal_id)
        org_id = row["org_id"]
        amount = row["amount_usdc"]
        dest_wallet = row["wallet"]

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                settled_ids, settled_total = await self._select_earnings_to_settle(
                    conn,
                    org_id=org_id,
                    amount_usdc=amount,
                )

                if settled_ids:
                    await conn.execute(
                        """
                        UPDATE tool_author_earnings
                        SET status = 'settled'
                        WHERE id = ANY($1::text[])
                        """,
                        settled_ids,
                    )

                tx_hash, transfer_failed, transfer_error = await self._attempt_transfer(
                    withdrawal_id=withdrawal_id,
                    dest_wallet=dest_wallet,
                    settled_total=settled_total,
                )

                now = datetime.now(timezone.utc)
                if transfer_failed:
                    if settled_ids:
                        await conn.execute(
                            """
                            UPDATE tool_author_earnings
                            SET status = 'pending'
                            WHERE id = ANY($1::text[])
                            """,
                            settled_ids,
                        )
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


def _get_withdrawal_service() -> _WithdrawalService:
    return _WithdrawalService(_get_pool(), get_settings())


async def request_withdrawal(org_id: str, amount_usdc: int) -> AuthorWithdrawal:
    """Request a validated withdrawal of accumulated earnings."""
    return await _get_withdrawal_service().request(org_id, amount_usdc)


async def process_withdrawal(withdrawal_id: str) -> AuthorWithdrawal:
    """Process a pending withdrawal and return the updated withdrawal model."""
    return await _get_withdrawal_service().process(withdrawal_id)


async def complete_withdrawal(withdrawal_id: str, tx_hash: str) -> None:
    """Record the on-chain transaction hash for a processed withdrawal."""
    pool = _get_pool()
    await pool.execute(
        "UPDATE tool_author_withdrawals SET tx_hash = $2 WHERE id = $1",
        withdrawal_id,
        tx_hash,
    )


async def list_pending_withdrawals(org_id: str | None = None) -> list[AuthorWithdrawal]:
    """List pending withdrawals. If org_id is None, returns all (admin use)."""
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
    """Reset a failed or exhausted withdrawal back to pending for re-processing."""
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


async def notify_subscribers_of_deactivation(
    qualified_tool_name: str,
    reason: str,
) -> None:
    """Email all admin/owner users of orgs subscribed to a deactivated tool."""
    from shared.email import send_tool_deactivated_email
    from teardrop.cache import get_redis

    settings = get_settings()
    redis = get_redis()
    dedup_key = f"teardrop:notify:tool_deact:{qualified_tool_name}"

    if redis is not None:
        try:
            already = await redis.set(dedup_key, "1", ex=3600, nx=True)
            if not already:
                logger.debug(
                    "notify_subscribers_of_deactivation: dedup skip qualified=%s",
                    qualified_tool_name,
                )
                return
        except Exception:
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
    """Mark a tool is_active=FALSE after the circuit breaker trips."""
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
    if result.split()[-1] == "0":
        return

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
        return

    await invalidate_marketplace_cache()

    if qualified_tool_name is None:
        org_row = await pool.fetchrow("SELECT slug FROM orgs WHERE id = $1", org_id)
        if org_row is None:
            return
        qualified_tool_name = f"{org_row['slug']}/{name}"

    await pool.execute(
        "UPDATE org_marketplace_subscriptions SET is_active = FALSE WHERE qualified_tool_name = $1 AND is_active = TRUE",
        qualified_tool_name,
    )

    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("tool_id", str(tool_id))
            scope.set_tag("org_id", str(org_id))
            scope.set_tag("circuit_breaker", "tripped")
            sentry_sdk.capture_message(
                f"Circuit breaker tripped: {qualified_tool_name}",
                level="warning",
            )
    except Exception:
        logger.debug("sentry capture failed in auto_deactivate", exc_info=True)

    try:
        asyncio.create_task(
            notify_subscribers_of_deactivation(
                qualified_tool_name,
                "automatic — repeated webhook failures",
            )
        )
    except Exception:
        logger.warning("Failed to schedule subscriber notification", exc_info=True)
