"""Unit tests for tool_health.py — the per-tool circuit breaker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tool_health


@pytest.fixture
def fake_redis():
    """An in-memory async-redis stand-in supporting the breaker calls."""
    store: dict[str, str] = {}
    ttl: dict[str, int] = {}

    redis = MagicMock()

    async def _get(key):
        return store.get(key)

    async def _set(key, value, *_, ex=None, nx=False, **__):
        if nx and key in store:
            return None
        store[key] = str(value)
        if ex is not None:
            ttl[key] = ex
        return True

    async def _setex(key, seconds, value):
        store[key] = str(value)
        ttl[key] = seconds
        return True

    async def _incr(key):
        cur = int(store.get(key, "0")) + 1
        store[key] = str(cur)
        return cur

    async def _expire(key, seconds):
        if key in store:
            ttl[key] = seconds
            return True
        return False

    async def _exists(key):
        return 1 if key in store else 0

    async def _delete(*keys):
        n = 0
        for k in keys:
            if k in store:
                del store[k]
                ttl.pop(k, None)
                n += 1
        return n

    redis.get = AsyncMock(side_effect=_get)
    redis.set = AsyncMock(side_effect=_set)
    redis.setex = AsyncMock(side_effect=_setex)
    redis.incr = AsyncMock(side_effect=_incr)
    redis.expire = AsyncMock(side_effect=_expire)
    redis.exists = AsyncMock(side_effect=_exists)
    redis.delete = AsyncMock(side_effect=_delete)

    redis._store = store
    redis._ttl = ttl
    return redis


@pytest.fixture
def breaker_settings():
    """Return a settings stub with breaker enabled and threshold=3."""
    s = MagicMock()
    s.tool_breaker_enabled = True
    s.tool_breaker_threshold = 3
    s.tool_breaker_window_seconds = 600
    return s


@pytest.mark.asyncio
async def test_record_failure_trips_at_threshold(fake_redis, breaker_settings):
    with patch("tool_health.get_redis", return_value=fake_redis), \
         patch("tool_health.get_settings", return_value=breaker_settings):
        # First two failures: not tripped.
        assert (await tool_health.record_failure("tool-1")) is False
        assert (await tool_health.record_failure("tool-1")) is False
        # Third failure crosses threshold.
        assert (await tool_health.record_failure("tool-1")) is True
        # Trip key now exists.
        assert (await tool_health.is_breaker_tripped("tool-1")) is True


@pytest.mark.asyncio
async def test_record_success_resets_counter(fake_redis, breaker_settings):
    with patch("tool_health.get_redis", return_value=fake_redis), \
         patch("tool_health.get_settings", return_value=breaker_settings):
        await tool_health.record_failure("tool-1")
        await tool_health.record_failure("tool-1")
        await tool_health.record_success("tool-1")
        # Counter cleared, next failure should not trip.
        assert (await tool_health.record_failure("tool-1")) is False


@pytest.mark.asyncio
async def test_is_breaker_tripped_when_no_trip(fake_redis, breaker_settings):
    with patch("tool_health.get_redis", return_value=fake_redis), \
         patch("tool_health.get_settings", return_value=breaker_settings):
        assert (await tool_health.is_breaker_tripped("tool-x")) is False


@pytest.mark.asyncio
async def test_clear_breaker_removes_trip_and_counter(fake_redis, breaker_settings):
    with patch("tool_health.get_redis", return_value=fake_redis), \
         patch("tool_health.get_settings", return_value=breaker_settings):
        for _ in range(3):
            await tool_health.record_failure("tool-1")
        assert (await tool_health.is_breaker_tripped("tool-1")) is True
        await tool_health.clear_breaker("tool-1")
        assert (await tool_health.is_breaker_tripped("tool-1")) is False


@pytest.mark.asyncio
async def test_breaker_disabled_is_no_op(fake_redis):
    s = MagicMock()
    s.tool_breaker_enabled = False
    s.tool_breaker_threshold = 3
    s.tool_breaker_window_seconds = 600
    with patch("tool_health.get_redis", return_value=fake_redis), \
         patch("tool_health.get_settings", return_value=s):
        # Many failures — never trips because breaker is disabled.
        for _ in range(10):
            tripped = await tool_health.record_failure("tool-1")
            assert tripped is False
        assert (await tool_health.is_breaker_tripped("tool-1")) is False


@pytest.mark.asyncio
async def test_fail_open_when_redis_unavailable(breaker_settings):
    with patch("tool_health.get_redis", return_value=None), \
         patch("tool_health.get_settings", return_value=breaker_settings):
        assert (await tool_health.record_failure("tool-1")) is False
        assert (await tool_health.is_breaker_tripped("tool-1")) is False
        # Should not raise.
        await tool_health.record_success("tool-1")
        await tool_health.clear_breaker("tool-1")
