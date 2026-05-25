# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Delegation billing subsystem for outbound A2A calls."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Awaitable, Callable

import asyncpg

logger = logging.getLogger(__name__)


class BillingDelegationService:
    """Encapsulates delegation preflight checks and ledger/audit writes."""

    def __init__(
        self,
        *,
        get_pool: Callable[[], asyncpg.Pool],
        get_settings: Callable[[], object],
        get_daily_debit_spend: Callable[[asyncpg.Connection | asyncpg.Pool, str], Awaitable[int]],
        debit_credit: Callable[[str, int, str], Awaitable[tuple[bool, int]]],
        get_live_pricing_for_model: Callable[..., Awaitable[object | None]],
    ):
        self._get_pool = get_pool
        self._get_settings = get_settings
        self._get_daily_debit_spend = get_daily_debit_spend
        self._debit_credit = debit_credit
        self._get_live_pricing_for_model = get_live_pricing_for_model

    async def check_delegation_budget(self, org_id: str, estimated_cost_usdc: int) -> str | None:
        """Return None when delegation is affordable, otherwise an error message."""
        settings = self._get_settings()
        if not settings.a2a_delegation_billing_enabled:
            return None

        cap = settings.a2a_delegation_max_cost_usdc
        if estimated_cost_usdc > cap:
            return f"Estimated delegation cost ({estimated_cost_usdc} atomic USDC) exceeds global cap ({cap})."

        pool = self._get_pool()
        row = await pool.fetchrow(
            "SELECT balance_usdc, spending_limit_usdc, is_paused FROM org_credits WHERE org_id = $1",
            org_id,
        )
        balance = int(row["balance_usdc"]) if row else 0
        spending_limit = int(row["spending_limit_usdc"]) if row else 0
        is_paused = bool(row["is_paused"]) if row else False

        if is_paused:
            return "Org billing is paused by admin. Contact your administrator."

        if balance < estimated_cost_usdc:
            return f"Insufficient credit for delegation: balance {balance} atomic USDC, estimated cost {estimated_cost_usdc}."

        if spending_limit > 0:
            daily_spend = await self._get_daily_debit_spend(pool, org_id)
            if daily_spend + estimated_cost_usdc > spending_limit:
                return f"Daily spending limit reached: {daily_spend} of {spending_limit} atomic USDC used in the last 24 hours."

        return None

    def apply_platform_fee(self, cost_usdc: int) -> int:
        """Add platform fee (basis points) to a delegation cost."""
        settings = self._get_settings()
        fee_bps = settings.a2a_delegation_platform_fee_bps
        return cost_usdc + (cost_usdc * fee_bps) // 10_000

    def get_byok_platform_fee(self, is_byok: bool) -> int:
        """Return flat per-run BYOK floor fee or 0."""
        if not is_byok:
            return 0
        return self._get_settings().byok_platform_fee_usdc

    async def calculate_byok_orchestration_cost(
        self,
        tokens_in: int,
        tokens_out: int,
        provider: str = "",
        model: str = "",
    ) -> int:
        """Compute BYOK orchestration fee from per-token pricing with a floor."""
        settings = self._get_settings()
        floor = settings.byok_platform_fee_usdc

        rule = await self._get_live_pricing_for_model(provider, model, is_byok=True)
        if rule is None:
            return floor

        computed = (tokens_in // 1000) * rule.tokens_in_cost_per_1k + (tokens_out // 1000) * rule.tokens_out_cost_per_1k
        return max(computed, floor)

    async def fund_delegation(self, org_id: str, cost_usdc: int, run_id: str, agent_url: str) -> bool:
        """Debit credit for an outbound delegation."""
        reason = f"a2a_delegation run={run_id} agent={agent_url}"
        success, _ = await self._debit_credit(org_id, cost_usdc, reason)
        return success

    async def record_delegation_event(
        self,
        org_id: str,
        run_id: str,
        agent_url: str,
        agent_name: str,
        task_status: str,
        cost_usdc: int,
        billing_method: str = "credit",
        settlement_tx: str = "",
        error: str = "",
    ) -> None:
        """Write immutable delegation event row (best effort)."""
        try:
            pool = self._get_pool()
            await pool.execute(
                """
                INSERT INTO a2a_delegation_events
                    (id, org_id, run_id, agent_url, agent_name,
                     task_status, cost_usdc, billing_method, settlement_tx, error, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                """,
                str(uuid.uuid4()),
                org_id,
                run_id,
                agent_url,
                agent_name,
                task_status,
                cost_usdc,
                billing_method,
                settlement_tx,
                error,
            )
        except Exception:
            logger.exception(
                "Failed to record delegation event org=%s run=%s agent=%s",
                org_id,
                run_id,
                agent_url,
            )

    async def get_delegation_events(
        self,
        org_id: str,
        limit: int = 50,
        cursor: datetime | None = None,
    ) -> list[dict]:
        """Return delegation events for an org (cursor-paginated, newest first)."""
        pool = self._get_pool()
        cursor_clause = "" if cursor is None else "AND created_at < $3"
        args: list = [org_id, limit, *([cursor] if cursor is not None else [])]
        rows = await pool.fetch(
            f"""
            SELECT id, org_id, run_id, agent_url, agent_name,
                   task_status, cost_usdc, billing_method, settlement_tx, error, created_at
            FROM a2a_delegation_events
            WHERE org_id = $1
              {cursor_clause}
            ORDER BY created_at DESC
            LIMIT $2
            """,
            *args,
        )
        return [dict(r) for r in rows]
