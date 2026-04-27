# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared Redis client singleton for rate limiting, SIWE nonces, and pricing cache.

Provides graceful degradation: if REDIS_URL is unset or connection fails,
all modules fall back to their in-process implementations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Generic, TypeVar

import redis.asyncio

logger = logging.getLogger(__name__)

T = TypeVar("T")

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


# ─── Generic single-value TTL cache ───────────────────────────────────────────


class TTLCache(Generic[T]):
    """Single-value TTL cache: Redis-first with in-process fallback.

    Preserves the existing graceful-degradation contract:
    - Redis is consulted first; on read failure we fall through to in-process.
    - In-process tier holds the last successfully-loaded value with a TTL.
    - Loader failures serve the stale in-process value if present, else the
      configured ``stale_default``.

    Designed for single-tenant caches (one logical value per cache instance).
    Keyed caches (e.g. per-org tool lists) instantiate one TTLCache per key.
    """

    def __init__(
        self,
        *,
        name: str,
        redis_key: str,
        ttl_seconds_fn: Callable[[], int],
        loader: Callable[[], Awaitable[T | None]],
        serialize: Callable[[T], str],
        deserialize: Callable[[str], T | None],
        cache_when: Callable[[T | None], bool] = lambda v: v is not None,
        stale_default: T | None = None,
    ) -> None:
        self._name = name
        self._redis_key = redis_key
        self._ttl_fn = ttl_seconds_fn
        self._loader = loader
        self._serialize = serialize
        self._deserialize = deserialize
        self._cache_when = cache_when
        self._stale_default = stale_default
        self._value: T | None = None
        self._expires: float = 0.0
        self._lock: asyncio.Lock | None = None

    async def get(self) -> T | None:
        # ── Redis path (multi-container) ──────────────────────────────────
        r = get_redis()
        if r is not None:
            try:
                raw = await r.get(self._redis_key)
                if raw is not None:
                    return self._deserialize(raw)
            except Exception as exc:
                logger.warning(
                    "Redis %s cache read failed; falling back to in-process: %s",
                    self._name,
                    exc,
                )

        # ── In-process fast path ──────────────────────────────────────────
        if self._value is not None and time.monotonic() < self._expires:
            return self._value

        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            # Double-check after acquiring; another coroutine may have refreshed.
            if self._value is not None and time.monotonic() < self._expires:
                return self._value

            try:
                value = await self._loader()
            except Exception:
                logger.warning(
                    "Failed to refresh %s cache; serving stale value",
                    self._name,
                    exc_info=True,
                )
                if self._value is not None:
                    return self._value
                return self._stale_default

            ttl = self._ttl_fn()
            if self._cache_when(value):
                self._value = value
                self._expires = time.monotonic() + ttl

                r = get_redis()
                if r is not None and value is not None:
                    try:
                        await r.setex(self._redis_key, ttl, self._serialize(value))
                    except Exception as exc:
                        logger.warning(
                            "Redis %s cache write failed (non-fatal): %s",
                            self._name,
                            exc,
                        )

            return value

    async def invalidate(self) -> None:
        """Drop the in-process value and delete the Redis key."""
        self._value = None
        self._expires = 0.0
        r = get_redis()
        if r is not None:
            try:
                await r.delete(self._redis_key)
            except Exception as exc:
                logger.warning(
                    "Redis %s cache invalidation failed (non-fatal): %s",
                    self._name,
                    exc,
                )

    def reset(self) -> None:
        """Synchronous reset of in-process tier only (for shutdown paths)."""
        self._value = None
        self._expires = 0.0
