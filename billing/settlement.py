# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Settlement retry queue helpers."""

from __future__ import annotations

import logging
import uuid

from billing.context import _get_daily_debit_spend, _get_daily_spend_cache, _get_pool
from billing.credit import BillingCreditService
from billing.history import record_settlement
from billing.models import BillingResult
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


def _get_credit_service() -> BillingCreditService:
    return BillingCreditService(
        get_pool=_get_pool,
        get_daily_spend_cache=_get_daily_spend_cache,
        get_daily_debit_spend_fn=_get_daily_debit_spend,
        billing_result_factory=BillingResult,
    )


async def enqueue_failed_settlement(
    usage_event_id: str,
    org_id: str,
    run_id: str,
    billing_method: str,
    amount_usdc: int,
    payment_payload: str | None = None,
) -> None:
    """Insert a failed settlement into the retry queue."""
    if amount_usdc <= 0:
        logger.debug(
            "Skipping enqueue_failed_settlement for non-positive amount: run_id=%s method=%s amount=%s",
            run_id,
            billing_method,
            amount_usdc,
        )
        return

    try:
        pool = _get_pool()
        settings = get_settings()
        await pool.execute(
            """
            INSERT INTO pending_settlements
                (id, usage_event_id, org_id, run_id, billing_method,
                 amount_usdc, payment_payload, max_retries, next_retry_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() + INTERVAL '2 seconds')
            """,
            str(uuid.uuid4()),
            usage_event_id,
            org_id,
            run_id,
            billing_method,
            amount_usdc,
            payment_payload,
            settings.settlement_max_retries,
        )
        logger.info(
            "Enqueued failed settlement for retry: run_id=%s method=%s",
            run_id,
            billing_method,
        )
    except Exception:
        logger.exception("Failed to enqueue settlement for retry: run_id=%s", run_id)


async def process_pending_settlements() -> int:
    """Process pending settlements that are due for retry."""
    pool = _get_pool()
    processed = 0

    try:
        rows = await pool.fetch(
            """
            SELECT id, usage_event_id, org_id, run_id, billing_method,
                   amount_usdc, payment_payload, retry_count, max_retries
            FROM pending_settlements
            WHERE status IN ('pending', 'retrying')
              AND next_retry_at <= NOW()
            ORDER BY next_retry_at
            LIMIT 20
            FOR UPDATE SKIP LOCKED
            """,
        )
    except Exception:
        logger.exception("Failed to query pending settlements")
        return 0

    for row in rows:
        settlement_id = row["id"]
        billing_method = row["billing_method"]
        retry_count = row["retry_count"] + 1
        max_retries = row["max_retries"]
        success = False
        settled_amount = 0
        error_msg = ""

        try:
            if billing_method == "credit":
                success, settled_amount = await _get_credit_service().debit_credit(
                    row["org_id"],
                    row["amount_usdc"],
                    reason=f"run:{row['run_id']}",
                )
                if not success:
                    error_msg = "debit_credit returned False"
            else:
                error_msg = "x402 settlements cannot be retried after initial failure"
                retry_count = max_retries
        except Exception as exc:
            error_msg = str(exc)
            logger.warning("Settlement retry failed: id=%s error=%s", settlement_id, exc)

        if success:
            await pool.execute(
                """
                UPDATE pending_settlements
                SET status = 'settled', retry_count = $2, last_error = ''
                WHERE id = $1
                """,
                settlement_id,
                retry_count,
            )
            await record_settlement(row["usage_event_id"], settled_amount, "", "settled")
            processed += 1
            logger.info(
                "Settlement retry succeeded: id=%s run_id=%s attempt=%d",
                settlement_id,
                row["run_id"],
                retry_count,
            )
        elif retry_count >= max_retries:
            await pool.execute(
                """
                UPDATE pending_settlements
                SET status = 'exhausted', retry_count = $2, last_error = $3
                WHERE id = $1
                """,
                settlement_id,
                retry_count,
                error_msg,
            )
            logger.error(
                "Settlement exhausted after %d retries: id=%s run_id=%s error=%s",
                retry_count,
                settlement_id,
                row["run_id"],
                error_msg,
            )
        else:
            backoff_seconds = min(2**retry_count, 300)
            await pool.execute(
                """
                UPDATE pending_settlements
                SET status = 'retrying',
                    retry_count = $2,
                    last_error = $3,
                    next_retry_at = NOW() + ($4 || ' seconds')::INTERVAL
                WHERE id = $1
                """,
                settlement_id,
                retry_count,
                error_msg,
                str(backoff_seconds),
            )

    return processed


async def get_pending_settlements(
    status_filter: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List pending/retrying/exhausted settlements (admin reconciliation)."""
    pool = _get_pool()
    params: list = [limit]
    where = ""
    if status_filter is not None:
        params.append(status_filter)
        where = f"WHERE status = ${len(params)}"
    rows = await pool.fetch(
        f"""
        SELECT id, usage_event_id, org_id, run_id, billing_method,
               amount_usdc, retry_count, max_retries, next_retry_at,
               last_error, status, created_at
        FROM pending_settlements
        {where}
        ORDER BY created_at DESC
        LIMIT $1
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def reset_exhausted_settlement(settlement_id: str) -> bool | None:
    """Reset an exhausted settlement back to pending (admin manual retry)."""
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT billing_method
        FROM pending_settlements
        WHERE id = $1 AND status = 'exhausted'
        """,
        settlement_id,
    )
    if row is None:
        return False
    if row["billing_method"] == "x402":
        return None

    result = await pool.execute(
        """
        UPDATE pending_settlements
        SET status = 'pending', retry_count = 0, next_retry_at = NOW()
        WHERE id = $1 AND status = 'exhausted'
        """,
        settlement_id,
    )
    return result == "UPDATE 1"
