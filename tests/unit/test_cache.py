# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for cache.py — Redis singleton initialization and graceful degradation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import teardrop.cache as cache_module


@pytest.mark.anyio
class TestInitRedis:
    async def test_init_redis_empty_url_leaves_none(self):
        """When REDIS_URL is empty, _redis remains None."""
        with patch.object(cache_module, "_redis", None):
            await cache_module.init_redis("")
            assert cache_module.get_redis() is None

    async def test_init_redis_successful_ping(self):
        """When connection succeeds, _redis is set to the client."""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value="PONG")

        with patch("redis.asyncio.from_url", new=AsyncMock(return_value=mock_client)):
            await cache_module.init_redis("redis://localhost:6379")
            assert cache_module.get_redis() is not None
            mock_client.ping.assert_called_once()

    async def test_init_redis_connection_failure_leaves_none(self):
        """When Redis connection fails, _redis remains None (graceful degradation)."""
        with patch("redis.asyncio.from_url", side_effect=Exception("Connection refused")):
            await cache_module.init_redis("redis://unreachable:6379")
            assert cache_module.get_redis() is None

    async def test_init_redis_ping_failure_leaves_none(self):
        """When ping fails, _redis remains None (graceful degradation)."""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=Exception("PING timeout"))

        with patch("redis.asyncio.from_url", return_value=mock_client):
            await cache_module.init_redis("redis://localhost:6379")
            assert cache_module.get_redis() is None

    async def test_required_redis_failure_aborts_startup(self):
        with patch("redis.asyncio.from_url", side_effect=Exception("Connection refused")):
            with pytest.raises(RuntimeError, match="Required Redis connection failed"):
                await cache_module.init_redis("redis://unreachable:6379", required=True)

    async def test_required_redis_rejects_empty_url(self):
        with pytest.raises(RuntimeError, match="REDIS_URL is required"):
            await cache_module.init_redis("", required=True)


@pytest.mark.anyio
class TestCloseRedis:
    async def test_close_redis_calls_aclose(self):
        """When _redis is not None, close_redis calls aclose."""
        mock_client = AsyncMock()
        with patch.object(cache_module, "_redis", mock_client):
            await cache_module.close_redis()
            mock_client.aclose.assert_called_once()
            # After closing, _redis should be None
            assert cache_module.get_redis() is None

    async def test_close_redis_when_none_is_noop(self):
        """When _redis is None, close_redis is a no-op (doesn't crash)."""
        with patch.object(cache_module, "_redis", None):
            await cache_module.close_redis()  # Should not raise
            assert cache_module.get_redis() is None


@pytest.mark.anyio
class TestGetRedis:
    async def test_get_redis_returns_client_when_set(self):
        """get_redis returns the client when it's been initialized."""
        mock_client = MagicMock()
        with patch.object(cache_module, "_redis", mock_client):
            assert cache_module.get_redis() is mock_client

    async def test_get_redis_returns_none_when_not_set(self):
        """get_redis returns None when Redis is disabled or failed to connect."""
        with patch.object(cache_module, "_redis", None):
            assert cache_module.get_redis() is None


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ["redis_read", "loader", "redis_write"])
async def test_ttl_cache_get_redacts_errors_without_changing_fallback(failure_stage, caplog):
    secret = "test-only-sensitive-cache-detail"
    loader = AsyncMock(return_value=50_000)
    redis_client = AsyncMock()
    redis_client.get.return_value = None
    if failure_stage == "redis_read":
        redis_client.get.side_effect = RuntimeError(secret)
    elif failure_stage == "loader":
        loader.side_effect = RuntimeError(secret)
    else:
        redis_client.setex.side_effect = RuntimeError(secret)
    pricing_cache = cache_module.TTLCache[int](
        name="pricing",
        redis_key="test:pricing",
        ttl_seconds_fn=lambda: 60,
        loader=loader,
        serialize=str,
        deserialize=int,
        stale_default=10_000,
    )

    with patch.object(cache_module, "_redis", redis_client), caplog.at_level("WARNING", logger="teardrop.cache"):
        result = await pricing_cache.get()

    assert result == (10_000 if failure_stage == "loader" else 50_000)
    loader.assert_awaited_once()
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
