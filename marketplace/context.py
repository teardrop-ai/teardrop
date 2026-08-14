# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace DB pool context helpers."""

from __future__ import annotations

import logging

from shared.db_pool import PgPool, bind_pool, require_pool, unbind_pool

logger = logging.getLogger(__name__)

_POOL_SCOPE = "marketplace"
_pool: PgPool | None = None


async def init_marketplace_db(pool: PgPool) -> None:
    """Store the Postgres pool reference. Called during app lifespan startup."""
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


def _get_pool() -> PgPool:
    return require_pool(
        _POOL_SCOPE,
        _pool,
        "Marketplace DB not initialised — call init_marketplace_db() first",
    )
