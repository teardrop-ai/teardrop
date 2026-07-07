"""Unit tests for the circuit-breaker observability hooks in tools.health.

Verifies that:
- Trip events emit a Sentry warning message.
- Redis-unavailable (fail-open) paths escalate to Sentry (error level).
- Sentry is NOT escalated for normal success/failure paths.
- ``is_breaker_tripped`` and ``clear_breaker`` also surface the fail-open
  condition to Sentry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sentry_sdk

import tools.health as tool_health


@pytest.fixture
def breaker_settings():
    s = MagicMock()
    s.tool_breaker_enabled = True
    s.tool_breaker_threshold = 3
    s.tool_breaker_window_seconds = 600
    return s


@pytest.fixture
def fake_redis():
    store: dict[str, str] = {}

    redis = MagicMock()

    async def _setex(key, _seconds, value):
        store[key] = str(value)
        return True

    async def _incr(key):
        cur = int(store.get(key, "0")) + 1
        store[key] = str(cur)
        return cur

    async def _expire(key, _seconds):
        return key in store

    async def _exists(key):
        return 1 if key in store else 0

    async def _delete(*keys):
        n = 0
        for k in keys:
            if k in store:
                del store[k]
                n += 1
        return n

    redis.setex = AsyncMock(side_effect=_setex)
    redis.incr = AsyncMock(side_effect=_incr)
    redis.expire = AsyncMock(side_effect=_expire)
    redis.exists = AsyncMock(side_effect=_exists)
    redis.delete = AsyncMock(side_effect=_delete)
    return redis


@pytest.mark.asyncio
async def test_record_failure_trip_emits_sentry_warning(breaker_settings, fake_redis):
    """A trip at the threshold must emit exactly one Sentry warning message."""
    with (
        patch.object(tool_health, "get_settings", return_value=breaker_settings),
        patch.object(tool_health, "get_redis", return_value=fake_redis),
        patch.object(sentry_sdk, "capture_message") as capture,
    ):
        # 3 failures, threshold=3 → trip on 3rd
        await tool_health.record_failure("tool-trip-1")
        await tool_health.record_failure("tool-trip-1")
        result = await tool_health.record_failure("tool-trip-1")

    assert result is True
    # Exactly one trip event should have been captured
    trip_captures = [call for call in capture.call_args_list if "circuit breaker tripped" in call.args[0]]
    assert len(trip_captures) == 1
    assert trip_captures[0].kwargs.get("level") == "warning"
    assert "tool-trip-1" in trip_captures[0].args[0]


@pytest.mark.asyncio
async def test_record_failure_below_threshold_no_sentry(breaker_settings, fake_redis):
    with (
        patch.object(tool_health, "get_settings", return_value=breaker_settings),
        patch.object(tool_health, "get_redis", return_value=fake_redis),
        patch.object(sentry_sdk, "capture_message") as capture,
    ):
        # Only 2 failures, threshold=3 — must NOT trip
        await tool_health.record_failure("tool-sub")
        await tool_health.record_failure("tool-sub")

    assert all("tripped" not in c.args[0] for c in capture.call_args_list)


@pytest.mark.asyncio
async def test_record_success_no_sentry(breaker_settings, fake_redis):
    with (
        patch.object(tool_health, "get_settings", return_value=breaker_settings),
        patch.object(tool_health, "get_redis", return_value=fake_redis),
        patch.object(sentry_sdk, "capture_message") as capture,
    ):
        await tool_health.record_success("tool-ok")

    capture.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_call",
    [
        ("record_success", lambda: tool_health.record_success("tool-x")),
        ("record_failure", lambda: tool_health.record_failure("tool-x")),
        ("is_breaker_tripped", lambda: tool_health.is_breaker_tripped("tool-x")),
        ("clear_breaker", lambda: tool_health.clear_breaker("tool-x")),
    ],
    ids=lambda x: x if isinstance(x, str) else "",
)
async def test_redis_unavailable_logs_error_and_escalates(caplog, breaker_settings, op_call):
    """Each breaker entry-point must emit a loud error log + Sentry capture when Redis is None.

    The caplog assertion is the contract test; the Sentry call is verified by a
    direct unit test in :func:`test_redis_unavailable_calls_sentry_capture_message`.
    """
    import logging

    op_name, op_coro_factory = op_call
    caplog.set_level(logging.ERROR, logger="tools.health")
    with (
        patch.object(tool_health, "get_settings", return_value=breaker_settings),
        patch.object(tool_health, "get_redis", return_value=None),
    ):
        await op_coro_factory()

    fail_open_records = [r for r in caplog.records if "DISABLED (fail-open)" in r.getMessage() and r.name == "tools.health"]
    assert len(fail_open_records) == 1, f"{op_name} should emit exactly one fail-open ERROR log when Redis is None"
    assert "tool-x" in fail_open_records[0].getMessage()


@pytest.mark.asyncio
async def test_redis_unavailable_calls_sentry_capture_message(breaker_settings):
    """Direct verification: _log_redis_unavailable invokes sentry_sdk.capture_message."""
    with (
        patch.object(tool_health, "get_settings", return_value=breaker_settings),
        patch.object(tool_health, "get_redis", return_value=None),
        patch.object(sentry_sdk, "capture_message") as capture,
    ):
        await tool_health.is_breaker_tripped("tool-direct")

    assert capture.call_count == 1
    call = capture.call_args
    assert "circuit breaker disabled" in call.args[0]
    assert "tool-direct" in call.args[0]
    assert call.kwargs.get("level") == "error"
