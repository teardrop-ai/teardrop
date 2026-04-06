# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Shared Redis client singleton for rate limiting, SIWE nonces, and pricing cache.

Provides graceful degradation: if REDIS_URL is unset or connection fails,
all modules fall back to their in-process implementations.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio

logger = logging.getLogger(__name__)

# ─── Redis client singleton ────────────────────────────────────────────────────

_redis: redis.asyncio.Redis[Any] | None = None


async def init_redis(url: str) -> None:
    """Initialize the Redis client. Gracefully degrades if URL is empty or connection fails.

    Args:
        url: Redis connection URL (redis://... or rediss://...), or empty string to skip.
    """
    global _redis

    if not url:
        logger.info("Redis disabled (REDIS_URL not set); using in-process fallbacks")
        _redis = None
        return

    try:
        client = await redis.asyncio.from_url(
            url,
            decode_responses=True,  # All values are strings (no binary surprises)
            socket_connect_timeout=2,  # Quick fail if Redis is unreachable
        )
        await client.ping()
        _redis = client
        logger.info("Redis client connected: %s", url.split("://")[0] + "://...")
    except Exception as exc:
        logger.warning("Redis connection failed (%s); falling back to in-process caches", exc)
        _redis = None


async def close_redis() -> None:
    """Close the Redis client connection."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        logger.info("Redis client closed")


def get_redis() -> redis.asyncio.Redis[Any] | None:
    """Return the Redis client, or None if not connected or disabled.

    Callers should check: if (redis := get_redis()) is not None: ...
    """
    return _redis
