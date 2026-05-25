# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace DB pool context helpers."""

from __future__ import annotations

import logging

import asyncpg

from shared.db_pool import bind_pool, require_pool, unbind_pool

logger = logging.getLogger(__name__)

_POOL_SCOPE = "marketplace"
_pool: asyncpg.Pool | None = None


async def init_marketplace_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference. Called during app lifespan startup."""
    global _pool
    _pool = bind_pool(_POOL_SCOPE, pool)
    logger.info("Marketplace DB ready")


async def close_marketplace_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        unbind_pool(_POOL_SCOPE)
        logger.info("Marketplace DB reference released")


def _get_pool() -> asyncpg.Pool:
    return require_pool(
        _POOL_SCOPE,
        _pool,
        "Marketplace DB not initialised — call init_marketplace_db() first",
    )
