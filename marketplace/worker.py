# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace earnings sweep worker."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from marketplace.catalog import get_author_config
from marketplace.context import _get_pool
from marketplace.withdrawals import process_withdrawal
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


def _sweep_withdrawal_id(org_id: str, epoch_hour: int) -> str:
    """Derive a deterministic withdrawal ID for a sweep cycle."""
    raw = hashlib.sha256(f"sweep:{org_id}:{epoch_hour}".encode()).digest()
    hex_str = raw[:16].hex()
    return f"{hex_str[:8]}-{hex_str[8:12]}-5{hex_str[13:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


def _sweep_backoff_seconds(attempt: int) -> int:
    """Exponential backoff for failed sweep attempts."""
    return min(2**attempt * 60, 86_400)


async def marketplace_sweep_once() -> int:
    """Auto-create and settle withdrawals for all qualifying orgs."""
    pool = _get_pool()
    settings = get_settings()
    min_amount = settings.marketplace_minimum_withdrawal_usdc
    max_retries = settings.marketplace_max_sweep_retries
    now = datetime.now(timezone.utc)
    epoch_hour = int(now.timestamp()) // 3600

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

    if settings.agent_wallet_enabled:
        try:
            from teardrop.agent_wallets import get_settlement_wallet_balance_usdc

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
    """Background task: periodically auto-process qualifying withdrawals."""
    settings = get_settings()
    interval = settings.marketplace_sweep_interval_seconds
    logger.info("marketplace_sweep_loop: started (interval=%ds)", interval)

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

        except Exception:
            cron_monitor = None

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("marketplace_sweep_loop: cancelled")
            raise
        cancel_exc: BaseException | None = None
        try:
            if cron_monitor is not None:
                with cron_monitor():
                    try:
                        settled = await marketplace_sweep_once()
                    except asyncio.CancelledError as exc:
                        cancel_exc = exc
                        settled = 0
            else:
                settled = await marketplace_sweep_once()

            if settled:
                logger.info("marketplace_sweep_loop: settled withdrawals=%d", settled)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("marketplace_sweep_loop: sweep failed")

        if cancel_exc is not None:
            raise cancel_exc
