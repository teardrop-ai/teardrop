# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Internal background task helpers for FastAPI app startup."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import asyncpg

from agent.cache_prewarm import prewarm_org_prefix
from billing import cleanup_expired_payment_nonces, process_pending_settlements
from teardrop.config import get_settings
from teardrop.llm_config import resolve_llm_config
from teardrop.memory import cleanup_expired_memories
from teardrop.users import cleanup_expired_refresh_tokens

settings = get_settings()
logger = logging.getLogger(__name__)


async def _run_periodic(
    name: str,
    coro_fn: Callable[[], Awaitable[Any]],
    interval: float,
    monitor_slug: str | None = None,
) -> None:
    """Run *coro_fn* every *interval* seconds with cancel + error handling."""
    monitor_cm = _build_cron_monitor(monitor_slug, interval)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        cancel_exc: BaseException | None = None
        try:
            if monitor_cm is not None:
                with monitor_cm():
                    try:
                        await coro_fn()
                    except asyncio.CancelledError as exc:
                        cancel_exc = exc
            else:
                await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s loop error", name)
        if cancel_exc is not None:
            raise cancel_exc


def _build_cron_monitor(slug: str | None, interval_seconds: float):
    """Return a zero-arg callable producing a Sentry cron context manager, or None."""
    if not slug or not settings.sentry_dsn:
        return None
    try:
        from sentry_sdk.crons import monitor as sentry_monitor
    except ImportError:  # pragma: no cover - sentry_sdk pinned in requirements
        return None

    minutes = max(1, int(interval_seconds // 60) or 1)
    monitor_config = {
        "schedule": {"type": "interval", "value": minutes, "unit": "minute"},
        "checkin_margin": max(2, minutes // 4 or 2),
        "max_runtime": max(2, minutes * 2),
        "failure_issue_threshold": 2,
        "recovery_threshold": 2,
    }

    def _factory():
        return sentry_monitor(monitor_slug=slug, monitor_config=monitor_config)

    return _factory


async def _settlement_retry_iter() -> None:
    processed = await process_pending_settlements()
    if processed:
        logger.info("Settlement retry: processed %d pending settlements", processed)


async def _memory_cleanup_iter() -> None:
    deleted = await cleanup_expired_memories()
    if deleted:
        logger.info("Memory cleanup: deleted %d expired memories", deleted)


async def _refresh_token_cleanup_iter() -> None:
    deleted = await cleanup_expired_refresh_tokens()
    if deleted:
        logger.info("Refresh token cleanup: deleted %d expired tokens", deleted)


async def _x402_nonce_cleanup_iter() -> None:
    deleted = await cleanup_expired_payment_nonces()
    if deleted:
        logger.info("x402 nonce cleanup: deleted %d expired payment claims", deleted)


async def _settlement_retry_loop() -> None:
    """Periodically retry failed settlements (runs as background task)."""
    await _run_periodic(
        "Settlement retry",
        _settlement_retry_iter,
        settings.settlement_retry_interval_seconds,
        monitor_slug="settlement-retry",
    )


async def _memory_cleanup_loop() -> None:
    """Periodically delete expired memories (runs as background task)."""
    await _run_periodic(
        "Memory cleanup",
        _memory_cleanup_iter,
        settings.memory_cleanup_interval_seconds,
        monitor_slug="memory-cleanup",
    )


async def _refresh_token_cleanup_loop() -> None:
    """Periodically delete revoked+expired refresh tokens (runs as background task)."""
    await _run_periodic(
        "Refresh token cleanup",
        _refresh_token_cleanup_iter,
        settings.refresh_token_cleanup_interval_seconds,
        monitor_slug="token-cleanup",
    )


async def _x402_nonce_cleanup_loop() -> None:
    """Periodically delete expired x402 payment-nonce claims (runs as background task)."""
    await _run_periodic(
        "x402 nonce cleanup",
        _x402_nonce_cleanup_iter,
        settings.refresh_token_cleanup_interval_seconds,
        monitor_slug="x402-nonce-cleanup",
    )


async def _prewarm_cache_prefixes(pool: asyncpg.Pool) -> None:
    """Warm provider prompt caches for the most active org/model prefixes."""
    if not settings.agent_cache_prewarm_enabled:
        return

    min_runs = max(1, int(settings.agent_cache_prewarm_min_runs_24h))
    top_n = max(1, int(settings.agent_cache_prewarm_top_n))

    try:
        rows = await pool.fetch(
            """
            SELECT org_id, provider, model, COUNT(*) AS run_count
            FROM usage_events
            WHERE created_at > NOW() - INTERVAL '24 hours'
              AND provider != ''
              AND model != ''
            GROUP BY org_id, provider, model
            HAVING COUNT(*) >= $1
            ORDER BY run_count DESC
            LIMIT $2
            """,
            min_runs,
            top_n,
        )
    except Exception:
        logger.debug("cache prewarm skipped: usage_events query failed", exc_info=True)
        return
    if not rows:
        return

    warmed = 0
    cache_creation_total = 0
    for row in rows:
        org_id = str(row["org_id"])
        provider = str(row["provider"])
        model = str(row["model"])

        llm_config = None
        try:
            resolved = await resolve_llm_config(org_id)
            if resolved and resolved.get("provider") == provider and resolved.get("model") == model:
                llm_config = resolved
        except Exception:
            logger.debug("cache prewarm: resolve_llm_config failed for org %s", org_id, exc_info=True)

        usage = await prewarm_org_prefix(org_id, provider, model, llm_config=llm_config)
        warmed += 1
        cache_creation_total += int(usage.get("cache_creation_input_tokens", 0))

    logger.info(
        "Cache prewarm completed: orgs_warmed=%d total_cache_creation_tokens=%d",
        warmed,
        cache_creation_total,
    )
