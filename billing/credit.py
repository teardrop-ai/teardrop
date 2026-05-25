# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Credit subsystem for billing (off-chain prepaid balances)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

import asyncpg
import sentry_sdk

from teardrop.cache import TTLCache

logger = logging.getLogger(__name__)


class BillingCreditService:
    """Encapsulates org credit balance verification and mutations."""

    def __init__(
        self,
        *,
        get_pool: Callable[[], asyncpg.Pool],
        get_daily_spend_cache: Callable[[str], TTLCache[int]],
        get_daily_debit_spend_fn: Callable[[asyncpg.Connection | asyncpg.Pool, str], Awaitable[int]],
        billing_result_factory: Callable[..., Any],
    ):
        self._get_pool = get_pool
        self._get_daily_spend_cache = get_daily_spend_cache
        self._get_daily_debit_spend_fn = get_daily_debit_spend_fn
        self._billing_result_factory = billing_result_factory

    async def get_credit_balance(self, org_id: str) -> int:
        """Return org's current credit balance in atomic USDC (0 if no row yet)."""
        pool = self._get_pool()
        row = await pool.fetchrow(
            "SELECT balance_usdc FROM org_credits WHERE org_id = $1",
            org_id,
        )
        return int(row["balance_usdc"]) if row is not None else 0

    async def get_daily_debit_spend(self, executor: asyncpg.Connection | asyncpg.Pool, org_id: str) -> int:
        """Return 24h rolling debit spend in atomic USDC for an org."""
        daily_row = await executor.fetchrow(
            """
            SELECT COALESCE(SUM(amount_usdc), 0) AS daily_spend
            FROM org_credit_ledger
            WHERE org_id = $1
              AND operation = 'debit'
              AND created_at >= NOW() - INTERVAL '24 hours'
            """,
            org_id,
        )
        return int(daily_row["daily_spend"]) if daily_row else 0

    async def verify_credit(self, org_id: str, min_balance_usdc: int) -> Any:
        """Check that org has sufficient credit and is within spending limits."""
        pool = self._get_pool()

        row = await pool.fetchrow(
            "SELECT balance_usdc, spending_limit_usdc, is_paused FROM org_credits WHERE org_id = $1",
            org_id,
        )
        balance = int(row["balance_usdc"]) if row else 0
        spending_limit = int(row["spending_limit_usdc"]) if row else 0
        is_paused = bool(row["is_paused"]) if row else False

        if is_paused:
            return self._billing_result_factory(error="Org billing is paused by admin. Contact your administrator.")

        if balance < min_balance_usdc:
            return self._billing_result_factory(
                error=(
                    f"Insufficient credit: balance {balance} atomic USDC, "
                    f"required {min_balance_usdc}. Top up via POST /admin/credits/topup."
                )
            )

        if spending_limit > 0:
            daily_spend = await self._get_daily_debit_spend_fn(pool, org_id)
            if daily_spend + min_balance_usdc > spending_limit:
                return self._billing_result_factory(
                    error=(
                        f"Daily spending limit reached: {daily_spend} of {spending_limit} "
                        "atomic USDC used in the last 24 hours."
                    )
                )

        return self._billing_result_factory(verified=True, billing_method="credit")

    async def debit_credit(self, org_id: str, amount_usdc: int, reason: str = "") -> tuple[bool, int]:
        """Debit amount_usdc from org's credit balance atomically."""
        if amount_usdc <= 0:
            logger.debug("debit_credit: skipping non-positive amount org_id=%s amount=%s", org_id, amount_usdc)
            return True, 0

        pool = self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT balance_usdc, spending_limit_usdc, is_paused FROM org_credits WHERE org_id = $1 FOR UPDATE",
                        org_id,
                    )
                    if row is None:
                        return False, 0
                    original_balance = int(row["balance_usdc"])
                    spending_limit = int(row["spending_limit_usdc"])
                    is_paused = bool(row["is_paused"])

                    if is_paused:
                        return False, 0

                    if spending_limit > 0:
                        daily_spend = await self._get_daily_debit_spend_fn(conn, org_id)
                        if daily_spend + amount_usdc > spending_limit:
                            return False, 0

                    new_balance = max(0, original_balance - amount_usdc)
                    actual_deducted = original_balance - new_balance
                    await conn.execute(
                        """
                        UPDATE org_credits
                        SET balance_usdc = $2, updated_at = NOW()
                        WHERE org_id = $1
                        """,
                        org_id,
                        new_balance,
                    )
                    await conn.execute(
                        """
                        INSERT INTO org_credit_ledger
                            (id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at)
                        VALUES ($1, $2, 'debit', $3, $4, $5, NOW())
                        """,
                        str(uuid.uuid4()),
                        org_id,
                        actual_deducted,
                        new_balance,
                        reason,
                    )
                await self._get_daily_spend_cache(org_id).invalidate()
            return True, actual_deducted
        except Exception as exc:
            logger.exception("debit_credit failed org_id=%s amount=%s", org_id, amount_usdc)
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("org_id", str(org_id))
                scope.set_tag("amount_usdc_atomic", str(amount_usdc))
                scope.set_tag("rail", "credit")
                sentry_sdk.capture_exception(exc)
            return False, 0

    async def admin_topup_credit(self, org_id: str, amount_usdc: int, reason: str = "") -> int:
        """Add amount_usdc to org's credit balance (upsert)."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO org_credits (org_id, balance_usdc, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (org_id) DO UPDATE
                        SET balance_usdc = org_credits.balance_usdc + EXCLUDED.balance_usdc,
                            updated_at = NOW()
                    RETURNING balance_usdc
                    """,
                    org_id,
                    amount_usdc,
                )
                new_balance = int(row["balance_usdc"])
                await conn.execute(
                    """
                    INSERT INTO org_credit_ledger
                        (id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at)
                    VALUES ($1, $2, 'topup', $3, $4, $5, NOW())
                    """,
                    str(uuid.uuid4()),
                    org_id,
                    amount_usdc,
                    new_balance,
                    reason,
                )
        return new_balance

    async def get_credit_history(
        self,
        org_id: str,
        operation: str | None = None,
        limit: int = 50,
        cursor: datetime | None = None,
    ) -> list[dict]:
        """Return credit ledger entries for an org (cursor paginated, newest first)."""
        pool = self._get_pool()
        params: list = [org_id, limit]
        filters = ["org_id = $1"]
        if operation is not None:
            params.append(operation)
            filters.append(f"operation = ${len(params)}")
        if cursor is not None:
            params.append(cursor)
            filters.append(f"created_at < ${len(params)}")
        where = " AND ".join(filters)
        rows = await pool.fetch(
            f"""
            SELECT id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at
            FROM org_credit_ledger
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT $2
            """,
            *params,
        )
        return [dict(r) for r in rows]
