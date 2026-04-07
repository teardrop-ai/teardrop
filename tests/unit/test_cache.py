"""Unit tests for cache.py — Redis singleton initialization and graceful degradation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cache as cache_module


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
