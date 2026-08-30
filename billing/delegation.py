# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Delegation billing subsystem for outbound A2A calls."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Awaitable, Callable

from shared.db_pool import PgConnection, PgPool

logger = logging.getLogger(__name__)

_DELEGATION_TASK_TYPES = frozenset({"general", "research", "analysis", "data_retrieval", "coding", "transaction", "automation"})
_DELEGATION_FAILURE_ORIGINS = frozenset({"unknown", "local", "remote"})
_DELIVERY_TRANSACTION_PATTERN = re.compile(r"^0x[a-fA-F0-9]{64}$")


def _normalize_task_type(task_type: str) -> str:
    normalized = str(task_type).strip().lower()
    return normalized if normalized in _DELEGATION_TASK_TYPES else "general"


def _normalize_failure_origin(failure_origin: str) -> str:
    normalized = str(failure_origin).strip().lower()
    return normalized if normalized in _DELEGATION_FAILURE_ORIGINS else "unknown"


def _normalize_delivery_transaction(transaction: str | None) -> str:
    value = str(transaction or "").strip()
    return value if not value or _DELIVERY_TRANSACTION_PATTERN.fullmatch(value) else ""


class BillingDelegationService:
    """Encapsulates delegation preflight checks and ledger/audit writes."""

    def __init__(
        self,
        *,
        get_pool: Callable[[], PgPool],
        get_settings: Callable[[], object],
        get_daily_debit_spend: Callable[[PgConnection | PgPool, str], Awaitable[int]],
        debit_credit: Callable[[str, int, str], Awaitable[tuple[bool, int]]],
        debit_credit_with_refund_outbox: Callable[..., Awaitable[tuple[bool, int]]] | None = None,
        get_live_pricing_for_model: Callable[..., Awaitable[object | None]],
        get_daily_principal_debit_spend: Callable[[PgConnection | PgPool, str, str], Awaitable[int]] | None = None,
    ):
        self._get_pool = get_pool
        self._get_settings = get_settings
        self._get_daily_debit_spend = get_daily_debit_spend
        self._get_daily_principal_debit_spend = get_daily_principal_debit_spend
        self._debit_credit = debit_credit
        self._debit_credit_with_refund_outbox = debit_credit_with_refund_outbox
        self._get_live_pricing_for_model = get_live_pricing_for_model

    async def check_delegation_budget(
        self,
        org_id: str,
        estimated_cost_usdc: int,
        *,
        principal_id: str | None = None,
    ) -> str | None:
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

        if principal_id:
            principal_limit = await pool.fetchrow(
                "SELECT daily_limit_usdc, is_paused FROM org_principal_spend_limits WHERE org_id = $1 AND principal_id = $2",
                org_id,
                principal_id,
            )
            if principal_limit is not None:
                if bool(principal_limit["is_paused"]):
                    return "Principal billing is paused by an administrator."
                if self._get_daily_principal_debit_spend is None:
                    return "Principal spending limit could not be verified."
                daily_spend = await self._get_daily_principal_debit_spend(pool, org_id, principal_id)
                principal_cap = int(principal_limit["daily_limit_usdc"])
                if daily_spend + estimated_cost_usdc > principal_cap:
                    return "Principal daily spending limit reached."

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

    async def fund_delegation(
        self,
        org_id: str,
        cost_usdc: int,
        run_id: str,
        agent_url: str,
        delegation_id: str,
        *,
        principal_id: str | None = None,
    ) -> bool:
        """Debit credit and persist refundable delegation state atomically."""
        if self._debit_credit_with_refund_outbox is None:
            logger.error(
                "Delegation funding callback is not configured; refusing org=%s delegation=%s",
                org_id,
                delegation_id,
            )
            return False

        reason = f"a2a_delegation run={run_id} agent={agent_url}"
        success, _ = await self._debit_credit_with_refund_outbox(
            org_id,
            cost_usdc,
            reason,
            delegation_id,
            run_id,
            principal_id=principal_id,
        )
        return success

    async def mark_delegation_possibly_delivered(self, org_id: str, delegation_id: str) -> bool:
        """Claim the ambiguous delivery state before an x402 retry is sent."""
        try:
            pool = self._get_pool()
            result = await pool.execute(
                """
                UPDATE a2a_delegation_refund_outbox
                SET delivery_status = 'possibly_delivered',
                    delivery_started_at = COALESCE(delivery_started_at, NOW()),
                    delivery_error = ''
                WHERE id = $1 AND org_id = $2
                  AND status IN ('pending', 'refund_requested')
                  AND delivery_status = 'not_attempted'
                """,
                delegation_id,
                org_id,
            )
            if result == "UPDATE 1":
                return True
            delivery_status = await pool.fetchval(
                "SELECT delivery_status FROM a2a_delegation_refund_outbox WHERE id = $1 AND org_id = $2",
                delegation_id,
                org_id,
            )
            return delivery_status in {"possibly_delivered", "confirmed"}
        except Exception:
            logger.exception("Failed to mark delegation delivery ambiguous org=%s delegation=%s", org_id, delegation_id)
            return False

    async def _complete_refund_locked(self, conn: PgConnection, org_id: str, delegation_id: str, row) -> bool:
        """Apply a refund while the caller owns the outbox row lock."""
        amount_usdc = int(row["amount_usdc"])
        credit_row = await conn.fetchrow(
            """
            UPDATE org_credits
            SET balance_usdc = balance_usdc + $2, updated_at = NOW()
            WHERE org_id = $1
            RETURNING balance_usdc
            """,
            org_id,
            amount_usdc,
        )
        if credit_row is None:
            raise RuntimeError("Credit account is missing for delegation refund")

        await conn.execute(
            """
            INSERT INTO org_credit_ledger
                (id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at)
            VALUES ($1, $2, 'topup', $3, $4, $5, NOW())
            """,
            str(uuid.uuid4()),
            org_id,
            amount_usdc,
            int(credit_row["balance_usdc"]),
            f"a2a:refund delegation={delegation_id} run={row['run_id']}",
        )
        await conn.execute(
            """
            UPDATE a2a_delegation_refund_outbox
            SET status = 'refunded', resolved_at = NOW()
            WHERE id = $1 AND org_id = $2
              AND status = 'refund_requested'
              AND delivery_status IN ('not_attempted', 'failed')
            """,
            delegation_id,
            org_id,
        )
        return True

    async def confirm_delegation_delivery(
        self,
        org_id: str,
        delegation_id: str,
        settlement_tx: str = "",
    ) -> bool:
        """Confirm delivery and cancel any not-yet-paid refund atomically."""
        settlement_tx = _normalize_delivery_transaction(settlement_tx)
        pool = self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        SELECT status, delivery_status
                        FROM a2a_delegation_refund_outbox
                        WHERE id = $1 AND org_id = $2
                        FOR UPDATE
                        """,
                        delegation_id,
                        org_id,
                    )
                    if row is None:
                        return False

                    refund_status = str(row["status"])
                    delivery_status = str(row["delivery_status"])
                    if delivery_status == "confirmed":
                        return refund_status != "refunded"
                    if delivery_status == "failed" or refund_status == "refunded":
                        return False
                    if delivery_status not in {"not_attempted", "possibly_delivered"}:
                        return False

                    await conn.execute(
                        """
                        UPDATE a2a_delegation_refund_outbox
                        SET delivery_status = 'confirmed',
                            delivery_resolved_at = COALESCE(delivery_resolved_at, NOW()),
                            delivery_settlement_tx = COALESCE(NULLIF($3, ''), delivery_settlement_tx),
                            delivery_error = ''
                        WHERE id = $1 AND org_id = $2
                        """,
                        delegation_id,
                        org_id,
                        settlement_tx,
                    )
                    if refund_status in {"pending", "refund_requested"}:
                        await conn.execute(
                            """
                            UPDATE a2a_delegation_refund_outbox
                            SET status = 'cancelled', resolved_at = COALESCE(resolved_at, NOW())
                            WHERE id = $1 AND org_id = $2
                              AND status IN ('pending', 'refund_requested')
                            """,
                            delegation_id,
                            org_id,
                        )
                    return True
        except Exception:
            logger.exception("Failed to confirm delegation delivery org=%s delegation=%s", org_id, delegation_id)
            return False

    async def fail_delegation_delivery(self, org_id: str, delegation_id: str, reason: str = "") -> bool:
        """Record definitive non-delivery and refund exactly once."""
        safe_reason = str(reason or "").strip()[:500]
        pool = self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        SELECT status, delivery_status, run_id, amount_usdc
                        FROM a2a_delegation_refund_outbox
                        WHERE id = $1 AND org_id = $2
                        FOR UPDATE
                        """,
                        delegation_id,
                        org_id,
                    )
                    if row is None:
                        return False

                    refund_status = str(row["status"])
                    delivery_status = str(row["delivery_status"])
                    if delivery_status == "confirmed" or refund_status == "cancelled":
                        return False
                    if delivery_status == "failed" and refund_status == "refunded":
                        return True
                    if refund_status == "refunded":
                        return False
                    if delivery_status not in {"possibly_delivered", "failed"}:
                        return False

                    await conn.execute(
                        """
                        UPDATE a2a_delegation_refund_outbox
                        SET delivery_status = 'failed',
                            delivery_resolved_at = COALESCE(delivery_resolved_at, NOW()),
                            delivery_error = $3
                        WHERE id = $1 AND org_id = $2
                        """,
                        delegation_id,
                        org_id,
                        safe_reason,
                    )
                    if refund_status == "pending":
                        await conn.execute(
                            """
                            UPDATE a2a_delegation_refund_outbox
                            SET status = 'refund_requested'
                            WHERE id = $1 AND org_id = $2 AND status = 'pending'
                            """,
                            delegation_id,
                            org_id,
                        )
                    return await self._complete_refund_locked(conn, org_id, delegation_id, row)
        except Exception:
            logger.exception("Failed to resolve delegation as not delivered org=%s delegation=%s", org_id, delegation_id)
            return False

    async def request_delegation_refund(self, org_id: str, delegation_id: str) -> bool:
        """Mark a funded delegation for refund; repeated requests are harmless."""
        try:
            pool = self._get_pool()
            row = await pool.fetchrow(
                """
                UPDATE a2a_delegation_refund_outbox
                SET status = 'refund_requested'
                                WHERE id = $1 AND org_id = $2 AND status = 'pending'
                                    AND delivery_status IN ('not_attempted', 'failed')
                RETURNING id
                """,
                delegation_id,
                org_id,
            )
            if row is not None:
                return True
            status = await pool.fetchval(
                "SELECT status FROM a2a_delegation_refund_outbox WHERE id = $1 AND org_id = $2",
                delegation_id,
                org_id,
            )
            return status in {"refund_requested", "refunded"}
        except Exception:
            logger.exception("Failed to request delegation refund org=%s delegation=%s", org_id, delegation_id)
            return False

    async def cancel_delegation_refund(self, org_id: str, delegation_id: str) -> bool:
        """Mark a successful delegation as non-refundable idempotently."""
        try:
            pool = self._get_pool()
            result = await pool.execute(
                """
                UPDATE a2a_delegation_refund_outbox
                SET status = 'cancelled',
                    resolved_at = COALESCE(resolved_at, NOW()),
                    delivery_status = CASE
                        WHEN delivery_status IN ('not_attempted', 'possibly_delivered') THEN 'confirmed'
                        ELSE delivery_status
                    END,
                    delivery_resolved_at = CASE
                        WHEN delivery_status IN ('not_attempted', 'possibly_delivered')
                            THEN COALESCE(delivery_resolved_at, NOW())
                        ELSE delivery_resolved_at
                    END,
                    delivery_error = ''
                WHERE id = $1 AND org_id = $2
                  AND status IN ('pending', 'refund_requested')
                  AND delivery_status IN ('not_attempted', 'possibly_delivered', 'confirmed')
                """,
                delegation_id,
                org_id,
            )
            if result == "UPDATE 1":
                return True
            status = await pool.fetchval(
                "SELECT status FROM a2a_delegation_refund_outbox WHERE id = $1 AND org_id = $2",
                delegation_id,
                org_id,
            )
            return status == "cancelled"
        except Exception:
            logger.exception("Failed to cancel delegation refund org=%s delegation=%s", org_id, delegation_id)
            return False

    async def complete_delegation_refund(self, org_id: str, delegation_id: str) -> bool:
        """Credit a requested refund and close it in one idempotent transaction."""
        pool = self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        SELECT run_id, amount_usdc
                        FROM a2a_delegation_refund_outbox
                                                WHERE id = $1 AND org_id = $2
                                                    AND status = 'refund_requested'
                                                    AND delivery_status IN ('not_attempted', 'failed')
                        FOR UPDATE
                        """,
                        delegation_id,
                        org_id,
                    )
                    if row is None:
                        status = await conn.fetchval(
                            "SELECT status FROM a2a_delegation_refund_outbox WHERE id = $1 AND org_id = $2",
                            delegation_id,
                            org_id,
                        )
                        return status == "refunded"

                    return await self._complete_refund_locked(conn, org_id, delegation_id, row)
        except Exception:
            logger.exception("Failed to complete delegation refund org=%s delegation=%s", org_id, delegation_id)
            return False

    async def process_delegation_refund_outbox(self, limit: int = 50) -> int:
        """Retry requested refunds and reconcile terminal events after a crash."""
        pool = self._get_pool()
        rows = await pool.fetch(
            """
            SELECT outbox.id, outbox.org_id, event.task_status, outbox.delivery_status
            FROM a2a_delegation_refund_outbox AS outbox
                LEFT JOIN a2a_delegation_events AS event
                    ON event.id = outbox.id AND event.org_id = outbox.org_id
            WHERE outbox.delivery_status <> 'possibly_delivered'
              AND (
                    outbox.status = 'refund_requested'
                    OR (
                        outbox.status = 'pending'
                        AND event.id IS NOT NULL
                        AND event.task_status <> 'possibly_delivered'
                    )
              )
            ORDER BY outbox.created_at
            LIMIT $1
            """,
            limit,
        )
        processed = 0
        for row in rows:
            if row.get("delivery_status") == "possibly_delivered" or row.get("task_status") == "possibly_delivered":
                continue
            if row["task_status"] == "completed":
                resolved = await self.cancel_delegation_refund(row["org_id"], row["id"])
            else:
                await self.request_delegation_refund(row["org_id"], row["id"])
                resolved = await self.complete_delegation_refund(row["org_id"], row["id"])
            if resolved:
                processed += 1
        return processed

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
        task_type: str = "general",
        delegation_id: str | None = None,
        failure_origin: str = "unknown",
    ) -> bool:
        """Write immutable delegation event row and report whether it succeeded."""
        try:
            pool = self._get_pool()
            event_id = delegation_id or str(uuid.uuid4())
            await pool.execute(
                """
                INSERT INTO a2a_delegation_events
                    (id, org_id, run_id, agent_url, agent_name,
                     task_status, cost_usdc, billing_method, settlement_tx, error, failure_origin, task_type, created_at)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW())
                ON CONFLICT (id) DO NOTHING
                """,
                event_id,
                org_id,
                run_id,
                agent_url,
                agent_name,
                task_status,
                cost_usdc,
                billing_method,
                settlement_tx,
                error,
                _normalize_failure_origin(failure_origin),
                _normalize_task_type(task_type),
            )
            return True
        except Exception:
            logger.exception(
                "Failed to record delegation event org=%s run=%s agent=%s",
                org_id,
                run_id,
                agent_url,
            )
            return False

    async def get_delegation_events(
        self,
        org_id: str,
        limit: int = 50,
        cursor: datetime | None = None,
    ) -> list[dict]:
        """Return delegation events for an org (cursor-paginated, newest first)."""
        pool = self._get_pool()
        cursor_clause = "" if cursor is None else "AND event.created_at < $3"
        args: list = [org_id, limit, *([cursor] if cursor is not None else [])]
        rows = await pool.fetch(
            f"""
            SELECT event.id, event.org_id, event.run_id, event.agent_url, event.agent_name,
                     event.task_status, event.cost_usdc, event.billing_method, event.settlement_tx,
                     event.error, event.task_type, event.created_at,
                     COALESCE(outbox.delivery_status, 'not_attempted') AS delivery_status,
                     outbox.delivery_resolved_at, outbox.delivery_settlement_tx, outbox.delivery_error
            FROM a2a_delegation_events AS event
            LEFT JOIN a2a_delegation_refund_outbox AS outbox
                ON outbox.id = event.id AND outbox.org_id = event.org_id
            WHERE event.org_id = $1
              {cursor_clause}
            ORDER BY event.created_at DESC
            LIMIT $2
            """,
            *args,
        )
        return [dict(r) for r in rows]

    async def get_possibly_delivered_delegations(
        self,
        org_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List paid delegations held for explicit operator reconciliation."""
        pool = self._get_pool()
        rows = await pool.fetch(
            """
            SELECT outbox.id, outbox.org_id, outbox.run_id, outbox.amount_usdc,
                   outbox.status AS refund_status, outbox.delivery_status,
                   outbox.delivery_started_at, outbox.delivery_resolved_at,
                   outbox.delivery_settlement_tx, outbox.delivery_error,
                   event.agent_url, event.agent_name, event.task_status,
                   event.task_type, event.billing_method, event.settlement_tx,
                   event.error, event.created_at
            FROM a2a_delegation_refund_outbox AS outbox
            LEFT JOIN a2a_delegation_events AS event
                ON event.id = outbox.id AND event.org_id = outbox.org_id
            WHERE outbox.delivery_status = 'possibly_delivered'
              AND ($1::text IS NULL OR outbox.org_id = $1)
            ORDER BY outbox.created_at DESC
            LIMIT $2
            """,
            org_id,
            min(max(limit, 1), 200),
        )
        return [dict(r) for r in rows]
