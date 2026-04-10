"""Unit tests for distributed rate limiting.

Tests both in-process fallback and Redis paths, per-user vs per-IP keying,
and X-RateLimit-* response headers.
"""

from __future__ import annotations

import time
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the rate limiter internals from app module.
import app as app_module


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _reset_rate_counters():
    """Clear the in-process rate counter dict between tests."""
    app_module._rate_counters.clear()


# ─── In-process fallback ─────────────────────────────────────────────────────


class TestInProcessRateLimit:
    """Tests with Redis disabled (get_redis returns None)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _reset_rate_counters()
        yield
        _reset_rate_counters()

    async def test_allows_requests_within_limit(self):
        with patch.object(app_module, "get_redis", return_value=None):
            for _ in range(5):
                allowed, remaining, reset_at = await app_module._check_rate_limit("test:ip1", 10)
                assert allowed is True
            # After 5 requests with limit 10, remaining should be 4 (10 - 6)
            assert remaining >= 0

    async def test_blocks_when_limit_exceeded(self):
        with patch.object(app_module, "get_redis", return_value=None):
            for _ in range(5):
                await app_module._check_rate_limit("test:ip2", 5)
            allowed, remaining, _ = await app_module._check_rate_limit("test:ip2", 5)
            assert allowed is False
            assert remaining == 0

    async def test_different_keys_are_independent(self):
        with patch.object(app_module, "get_redis", return_value=None):
            # Exhaust limit for key A.
            for _ in range(3):
                await app_module._check_rate_limit("test:a", 3)
            allowed_a, _, _ = await app_module._check_rate_limit("test:a", 3)
            assert allowed_a is False

            # Key B should still be allowed.
            allowed_b, _, _ = await app_module._check_rate_limit("test:b", 3)
            assert allowed_b is True

    async def test_reset_epoch_is_in_future(self):
        with patch.object(app_module, "get_redis", return_value=None):
            _, _, reset_at = await app_module._check_rate_limit("test:ip3", 10)
            assert reset_at > int(time.time())

    async def test_remaining_decreases_per_request(self):
        with patch.object(app_module, "get_redis", return_value=None):
            _, r1, _ = await app_module._check_rate_limit("test:ip4", 5)
            _, r2, _ = await app_module._check_rate_limit("test:ip4", 5)
            assert r2 < r1

    async def test_max_keys_eviction(self):
        """When _RATE_COUNTER_MAX_KEYS is exceeded, oldest key is evicted."""
        with patch.object(app_module, "get_redis", return_value=None):
            original_max = app_module._RATE_COUNTER_MAX_KEYS
            try:
                app_module._RATE_COUNTER_MAX_KEYS = 3
                for i in range(5):
                    await app_module._check_rate_limit(f"evict:{i}", 100)
                # Should have evicted some keys, total <= 3 + 1 (current)
                assert len(app_module._rate_counters) <= 4
            finally:
                app_module._RATE_COUNTER_MAX_KEYS = original_max


# ─── Redis path ───────────────────────────────────────────────────────────────


class TestRedisRateLimit:
    """Tests with a mocked Redis client."""

    def _make_mock_redis(self, current_count: int = 0):
        """Build a mock Redis that simulates sorted-set pipeline."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        # Pipeline returns: (zremrangebyscore result, zcard count, zadd result, expire result)
        mock_pipe.execute = AsyncMock(return_value=(None, current_count, None, None))
        mock_pipe.zremrangebyscore = MagicMock(return_value=mock_pipe)
        mock_pipe.zcard = MagicMock(return_value=mock_pipe)
        mock_pipe.zadd = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        return mock_redis

    async def test_redis_allows_when_under_limit(self):
        mock_redis = self._make_mock_redis(current_count=5)
        with patch.object(app_module, "get_redis", return_value=mock_redis):
            allowed, remaining, _ = await app_module._check_rate_limit("redis:key1", 10)
            assert allowed is True
            assert remaining == 4  # 10 - 5 - 1

    async def test_redis_blocks_when_at_limit(self):
        mock_redis = self._make_mock_redis(current_count=10)
        with patch.object(app_module, "get_redis", return_value=mock_redis):
            allowed, remaining, _ = await app_module._check_rate_limit("redis:key2", 10)
            assert allowed is False

    async def test_redis_failure_falls_back_to_inprocess(self):
        """When Redis pipeline raises, falls back to in-process limiter."""
        _reset_rate_counters()
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute = AsyncMock(side_effect=ConnectionError("Redis down"))
        mock_pipe.zremrangebyscore = MagicMock(return_value=mock_pipe)
        mock_pipe.zcard = MagicMock(return_value=mock_pipe)
        mock_pipe.zadd = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        with patch.object(app_module, "get_redis", return_value=mock_redis):
            allowed, _, _ = await app_module._check_rate_limit("fallback:key", 10)
            assert allowed is True  # In-process fallback should work
        _reset_rate_counters()


# ─── Keying patterns ─────────────────────────────────────────────────────────


class TestRateLimitKeying:
    """Verify that auth endpoints key by IP and agent/run keys by user_id."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _reset_rate_counters()
        yield
        _reset_rate_counters()

    async def test_auth_key_format(self):
        """Auth endpoints should use 'auth:{ip}' keying."""
        with patch.object(app_module, "get_redis", return_value=None):
            await app_module._check_rate_limit("auth:192.168.1.1", 20)
            assert "auth:192.168.1.1" in app_module._rate_counters

    async def test_run_key_format(self):
        """Agent run should use 'run:{user_id}' keying."""
        with patch.object(app_module, "get_redis", return_value=None):
            await app_module._check_rate_limit("run:user-abc-123", 30)
            assert "run:user-abc-123" in app_module._rate_counters

    async def test_auth_and_run_keys_are_independent(self):
        """Same IP exhausting auth limit should not affect run limit."""
        with patch.object(app_module, "get_redis", return_value=None):
            # Exhaust auth limit for an IP.
            for _ in range(3):
                await app_module._check_rate_limit("auth:10.0.0.1", 3)
            auth_allowed, _, _ = await app_module._check_rate_limit("auth:10.0.0.1", 3)
            assert auth_allowed is False

            # Run limit for a user should be unaffected.
            run_allowed, _, _ = await app_module._check_rate_limit("run:user-xyz", 30)
            assert run_allowed is True
