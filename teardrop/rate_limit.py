# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Sliding-window rate limiting (Redis-first with in-process fallback).

Extracted verbatim from ``teardrop.app`` so that router modules can share the
same limiter without importing the application module. The in-process
``_rate_counters`` dict is a single module-level instance; all importers share
it, preserving the original single-container fallback semantics.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, status

from teardrop.cache import get_redis

logger = logging.getLogger(__name__)

# ─── Rate limiting (sliding-window, Redis-first with in-process fallback) ─────

_rate_counters: dict[str, list[float]] = defaultdict(list)
_RATE_COUNTER_MAX_KEYS = 10_000

# Named tuple for rate limit check results.
RateLimitResult = tuple[bool, int, int]  # (allowed, remaining, reset_epoch)


async def _check_rate_limit(key: str, limit: int) -> RateLimitResult:
    """Check sliding-window rate limit for *key*.

    Returns ``(allowed, remaining, reset_epoch)``:
    - *allowed*: ``True`` when within limit.
    - *remaining*: requests left in the current window.
    - *reset_epoch*: Unix timestamp when the window resets.

    Uses Redis sorted sets when available; falls back to in-process dict.
    """
    now = time.time()
    window = 60.0
    reset_epoch = int(now + window)

    # ── Redis path (multi-container) ──────────────────────────────────────
    if (redis := get_redis()) is not None:
        redis_key = f"teardrop:rl:{key}"
        try:
            pipe = redis.pipeline()
            pipe.zremrangebyscore(redis_key, "-inf", f"({now - window}")
            pipe.zcard(redis_key)
            pipe.zadd(redis_key, {f"{now}_{secrets.token_hex(3)}": now})
            pipe.expire(redis_key, 61)
            _, count, _, _ = await pipe.execute()
            remaining = max(0, limit - count - 1)
            return count < limit, remaining, reset_epoch
        except Exception as exc:
            logger.warning("Redis rate limit check failed; falling back to in-process: %s", exc)

    # ── In-process fallback (single-container) ───────────────────────────
    history = _rate_counters[key]
    _rate_counters[key] = [t for t in history if now - t < window]
    if len(_rate_counters[key]) >= limit:
        return False, 0, reset_epoch
    _rate_counters[key].append(now)
    remaining = max(0, limit - len(_rate_counters[key]))
    if len(_rate_counters) > _RATE_COUNTER_MAX_KEYS:
        oldest_key = next(iter(_rate_counters))
        del _rate_counters[oldest_key]
    return True, remaining, reset_epoch


async def _enforce_rate_limit(
    key: str,
    limit: int,
    *,
    detail: str = "Rate limit exceeded.",
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Check sliding-window limit and raise HTTPException(429) on breach.

    Emits the standard ``X-RateLimit-*`` and ``Retry-After`` headers. Callers
    that need a non-standard response shape (e.g. webhooks returning
    ``JSONResponse`` 429) should continue to call ``_check_rate_limit``
    directly.
    """
    allowed, remaining, reset_at = await _check_rate_limit(key, limit)
    if allowed:
        return
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_at),
        "Retry-After": "60",
    }
    if extra_headers:
        headers.update(extra_headers)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers=headers,
    )
