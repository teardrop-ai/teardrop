# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Billing DB context, cache helpers, and spending config helpers."""

from __future__ import annotations

import asyncpg

from shared.db_pool import bind_pool, require_pool, unbind_pool
from teardrop.cache import TTLCache

_POOL_SCOPE = "billing"
_pool: asyncpg.Pool | None = None
_daily_spend_caches: dict[str, TTLCache[int]] = {}


def _bind_pool(pool: asyncpg.Pool) -> asyncpg.Pool:
    """Bind and store the billing DB pool for this process."""
    global _pool
    _pool = bind_pool(_POOL_SCOPE, pool)
    return _pool


def _has_pool() -> bool:
    return _pool is not None


def _get_pool() -> asyncpg.Pool:
    return require_pool(_POOL_SCOPE, _pool, "Billing DB not initialised")


def _clear_pool() -> None:
    """Clear and unbind the billing DB pool."""
    global _pool
    _pool = None
    unbind_pool(_POOL_SCOPE)


async def _get_daily_debit_spend(executor: asyncpg.Connection | asyncpg.Pool, org_id: str) -> int:
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


def _get_daily_spend_cache(org_id: str) -> TTLCache[int]:
    """Return per-org cache for 24h debit spend used by display endpoints."""
    if org_id not in _daily_spend_caches:
        _daily_spend_caches[org_id] = TTLCache(
            name=f"daily spend:{org_id}",
            redis_key=f"teardrop:daily_spend:{org_id}",
            ttl_seconds_fn=lambda: 30,
            loader=lambda: _get_daily_debit_spend(_get_pool(), org_id),
            serialize=lambda v: str(v),
            deserialize=lambda raw: int(raw),
            stale_default=0,
        )
    return _daily_spend_caches[org_id]


def _reset_daily_spend_caches() -> None:
    for cache in _daily_spend_caches.values():
        cache.reset()
    _daily_spend_caches.clear()


async def get_org_spending_config(org_id: str) -> dict:
    """Return spending config for an org (balance, limit, pause state, daily spend)."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT balance_usdc, spending_limit_usdc, is_paused FROM org_credits WHERE org_id = $1",
        org_id,
    )
    balance = int(row["balance_usdc"]) if row else 0
    spending_limit = int(row["spending_limit_usdc"]) if row else 0
    is_paused = bool(row["is_paused"]) if row else False

    # 24-hour rolling window daily spend cached for dashboard-style reads.
    daily_spend = (await _get_daily_spend_cache(org_id).get()) or 0

    return {
        "org_id": org_id,
        "balance_usdc": balance,
        "spending_limit_usdc": spending_limit,
        "is_paused": is_paused,
        "daily_spend_usdc": daily_spend,
    }


async def update_org_spending_config(
    org_id: str,
    spending_limit_usdc: int | None = None,
    is_paused: bool | None = None,
) -> dict | None:
    """Update spending limit and/or pause state for an org.

    Returns updated config dict, or None if org_credits row doesn't exist.
    """
    pool = _get_pool()
    updates = []
    params: list = [org_id]

    if spending_limit_usdc is not None:
        params.append(spending_limit_usdc)
        updates.append(f"spending_limit_usdc = ${len(params)}")
    if is_paused is not None:
        params.append(is_paused)
        updates.append(f"is_paused = ${len(params)}")

    if not updates:
        return await get_org_spending_config(org_id)

    updates.append("updated_at = NOW()")
    set_clause = ", ".join(updates)

    result = await pool.execute(
        f"UPDATE org_credits SET {set_clause} WHERE org_id = $1",
        *params,
    )
    if result == "UPDATE 0":
        return None
    return await get_org_spending_config(org_id)
