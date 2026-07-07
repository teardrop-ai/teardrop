# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Redis-backed circuit breaker for marketplace / org-tool webhooks.

Single responsibility: count consecutive failures inside a sliding window and
report when a tool should be auto-deactivated.  All state lives in Redis under
``teardrop:tool_health:fail:{tool_id}``.  The window is enforced by a TTL on
the counter key — when the TTL elapses, the counter naturally resets.

If Redis is not configured (``get_redis()`` returns ``None``) the breaker is a
no-op (fail-open).  This preserves the current behaviour for development and
single-process deployments without Redis.
"""

from __future__ import annotations

import logging

import sentry_sdk

from teardrop.cache import get_redis
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


def _log_redis_unavailable(op: str, tool_id: str, exc: BaseException | None = None) -> None:
    """Surface breaker disablement loudly — the breaker is fail-open without Redis."""
    logger.error(
        "tool_health.%s: Redis unavailable — circuit breaker is DISABLED (fail-open) tool_id=%s",
        op,
        tool_id,
    )
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", "tool_health")
        scope.set_tag("tool_id", str(tool_id))
        scope.set_tag("redis_op", op)
        sentry_sdk.capture_message(
            f"tool_health: Redis unavailable; circuit breaker disabled for tool_id={tool_id}",
            level="error",
        )


def _fail_key(tool_id: str) -> str:
    return f"teardrop:tool_health:fail:{tool_id}"


def _trip_key(tool_id: str) -> str:
    return f"teardrop:tool_health:trip:{tool_id}"


async def record_success(tool_id: str) -> None:
    """Clear the failure counter for a tool after a successful call."""
    if not get_settings().tool_breaker_enabled:
        return
    redis = get_redis()
    if redis is None:
        _log_redis_unavailable("record_success", tool_id)
        return
    try:
        await redis.delete(_fail_key(tool_id))
    except Exception as exc:  # pragma: no cover — best effort
        logger.warning("tool_health.record_success redis error tool_id=%s: %s", tool_id, exc)


async def record_failure(tool_id: str) -> bool:
    """Increment the failure counter; return True if the breaker should trip.

    The trip is also recorded as a separate flag with a TTL equal to the
    failure window so callers can use ``is_breaker_tripped`` as a quick
    pre-execution gate.
    """
    settings = get_settings()
    if not settings.tool_breaker_enabled:
        return False
    redis = get_redis()
    if redis is None:
        _log_redis_unavailable("record_failure", tool_id)
        return False

    threshold = settings.tool_breaker_threshold
    window = settings.tool_breaker_window_seconds
    key = _fail_key(tool_id)

    try:
        # INCR is atomic; EXPIRE only sets TTL if key did not previously exist.
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window)
        if count >= threshold:
            await redis.setex(_trip_key(tool_id), window, "1")
            sentry_sdk.capture_message(
                f"tool_health: circuit breaker tripped tool_id={tool_id} count={count} threshold={threshold} window={window}s",
                level="warning",
            )
            return True
    except Exception as exc:  # pragma: no cover — best effort
        logger.warning("tool_health.record_failure redis error tool_id=%s: %s", tool_id, exc)
        return False
    return False


async def is_breaker_tripped(tool_id: str) -> bool:
    """Quick pre-execution check.  Returns True if the tool was recently tripped."""
    if not get_settings().tool_breaker_enabled:
        return False
    redis = get_redis()
    if redis is None:
        _log_redis_unavailable("is_breaker_tripped", tool_id)
        return False
    try:
        return bool(await redis.exists(_trip_key(tool_id)))
    except Exception as exc:  # pragma: no cover
        logger.warning("tool_health.is_breaker_tripped redis error tool_id=%s: %s", tool_id, exc)
        return False


async def clear_breaker(tool_id: str) -> None:
    """Clear both the failure counter and trip flag.  Called on manual re-enable."""
    if not get_settings().tool_breaker_enabled:
        return
    redis = get_redis()
    if redis is None:
        _log_redis_unavailable("clear_breaker", tool_id)
        return
    try:
        await redis.delete(_fail_key(tool_id), _trip_key(tool_id))
    except Exception as exc:  # pragma: no cover
        logger.warning("tool_health.clear_breaker redis error tool_id=%s: %s", tool_id, exc)
