# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scheduled runs DB pool context helpers."""

from __future__ import annotations

import logging

import asyncpg

from shared.db_pool import bind_pool, require_pool, unbind_pool

logger = logging.getLogger(__name__)

_POOL_SCOPE = "scheduling"
_pool: asyncpg.Pool | None = None


async def init_scheduling_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference. Called during app lifespan startup."""
    global _pool
    _pool = bind_pool(_POOL_SCOPE, pool)
    logger.info("Scheduling DB ready")


async def close_scheduling_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        unbind_pool(_POOL_SCOPE)
        logger.info("Scheduling DB reference released")


def _get_pool() -> asyncpg.Pool:
    return require_pool(
        _POOL_SCOPE,
        _pool,
        "Scheduling DB not initialised — call init_scheduling_db() first",
    )
