"""Unit tests for wallets.py — DB functions mocked via pool MagicMock."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import wallets as wallets_module
from wallets import Wallet

# ─── Pool mock helper ─────────────────────────────────────────────────────────


def _pool():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock(return_value="")
    return pool


def _wallet_row(**overrides):
    defaults = {
        "id": "w-1",
        "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        "chain_id": 1,
        "user_id": "user-1",
        "org_id": "org-1",
        "is_primary": False,
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return defaults


# ─── create_wallet ────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCreateWallet:
    async def test_returns_wallet_model(self):
        from wallets import create_wallet

        pool = _pool()
        with patch.object(wallets_module, "_pool", pool):
            wallet = await create_wallet(
                address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
                chain_id=1,
                user_id="user-1",
                org_id="org-1",
            )
        assert isinstance(wallet, Wallet)
        assert wallet.chain_id == 1
        pool.execute.assert_called_once()

    async def test_db_error_propagates(self):
        from wallets import create_wallet

        pool = _pool()
        pool.execute = AsyncMock(side_effect=Exception("unique constraint"))
        with patch.object(wallets_module, "_pool", pool):
            with pytest.raises(Exception, match="unique constraint"):
                await create_wallet("0xabc", 1, "user-1", "org-1")


# ─── get_wallet_by_address ────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetWalletByAddress:
    async def test_returns_wallet_when_found(self):
        from wallets import get_wallet_by_address

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=_wallet_row())
        with patch.object(wallets_module, "_pool", pool):
            wallet = await get_wallet_by_address("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", 1)
        assert wallet is not None
        assert wallet.id == "w-1"

    async def test_returns_none_when_not_found(self):
        from wallets import get_wallet_by_address

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(wallets_module, "_pool", pool):
            wallet = await get_wallet_by_address("0xunknown", 1)
        assert wallet is None


# ─── get_wallets_by_user ──────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetWalletsByUser:
    async def test_returns_list_of_wallets(self):
        from wallets import get_wallets_by_user

        pool = _pool()
        pool.fetch = AsyncMock(return_value=[_wallet_row(), _wallet_row(id="w-2")])
        with patch.object(wallets_module, "_pool", pool):
            wallets = await get_wallets_by_user("user-1")
        assert len(wallets) == 2
        assert all(isinstance(w, Wallet) for w in wallets)

    async def test_returns_empty_when_none(self):
        from wallets import get_wallets_by_user

        pool = _pool()
        pool.fetch = AsyncMock(return_value=[])
        with patch.object(wallets_module, "_pool", pool):
            wallets = await get_wallets_by_user("user-nobody")
        assert wallets == []


# ─── delete_wallet ────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestDeleteWallet:
    async def test_returns_true_when_deleted(self):
        from wallets import delete_wallet

        pool = _pool()
        pool.execute = AsyncMock(return_value="DELETE 1")
        with patch.object(wallets_module, "_pool", pool):
            result = await delete_wallet("w-1", "user-1")
        assert result is True

    async def test_returns_false_when_not_found(self):
        from wallets import delete_wallet

        pool = _pool()
        pool.execute = AsyncMock(return_value="DELETE 0")
        with patch.object(wallets_module, "_pool", pool):
            result = await delete_wallet("w-missing", "user-1")
        assert result is False


# ─── Nonce management ────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestNonces:
    async def test_create_nonce_returns_string(self):
        from wallets import create_nonce

        pool = _pool()
        with patch.object(wallets_module, "_pool", pool):
            nonce = await create_nonce()
        assert isinstance(nonce, str)
        assert len(nonce) >= 8
        pool.execute.assert_called_once()

    async def test_consume_valid_nonce_returns_true(self):
        from wallets import consume_nonce

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value={"nonce": "valid-nonce"})
        with patch.object(wallets_module, "_pool", pool):
            result = await consume_nonce("valid-nonce")
        assert result is True

    async def test_consume_expired_or_missing_nonce_returns_false(self):
        from wallets import consume_nonce

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(wallets_module, "_pool", pool):
            result = await consume_nonce("expired-nonce")
        assert result is False

    async def test_create_nonce_uses_redis_when_available(self):
        """When Redis is available, create_nonce stores the nonce in Redis."""
        from wallets import create_nonce

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        pool = _pool()

        with patch("wallets.get_redis", return_value=mock_redis):
            with patch.object(wallets_module, "_pool", pool):
                nonce = await create_nonce()

        assert isinstance(nonce, str)
        mock_redis.set.assert_called_once()
        # Verify the call used the correct key prefix
        call_args = mock_redis.set.call_args
        assert call_args[0][0].startswith("teardrop:nonce:")
        # Pool should not have been called when Redis succeeded
        pool.execute.assert_not_called()

    async def test_create_nonce_falls_back_to_postgres_on_redis_failure(self):
        """When Redis fails, create_nonce falls back to Postgres."""
        from wallets import create_nonce

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=Exception("Redis error"))
        pool = _pool()

        with patch("wallets.get_redis", return_value=mock_redis):
            with patch.object(wallets_module, "_pool", pool):
                nonce = await create_nonce()

        assert isinstance(nonce, str)
        # Postgres should have been called as fallback
        pool.execute.assert_called_once()

    async def test_consume_nonce_uses_redis_when_available(self):
        """When Redis is available, consume_nonce uses GETDEL."""
        from wallets import consume_nonce

        mock_redis = AsyncMock()
        mock_redis.getdel = AsyncMock(return_value="1")  # Nonce exists
        pool = _pool()

        with patch("wallets.get_redis", return_value=mock_redis):
            with patch.object(wallets_module, "_pool", pool):
                result = await consume_nonce("test-nonce")

        assert result is True
        mock_redis.getdel.assert_called_once_with("teardrop:nonce:test-nonce")
        # Pool should not have been called when Redis succeeded
        pool.fetchrow.assert_not_called()

    async def test_consume_nonce_redis_already_consumed(self):
        """When Redis returns None (already consumed), consume_nonce returns False."""
        from wallets import consume_nonce

        mock_redis = AsyncMock()
        mock_redis.getdel = AsyncMock(return_value=None)  # Already consumed
        pool = _pool()

        with patch("wallets.get_redis", return_value=mock_redis):
            with patch.object(wallets_module, "_pool", pool):
                result = await consume_nonce("used-nonce")

        assert result is False
        pool.fetchrow.assert_not_called()

    async def test_consume_nonce_falls_back_to_postgres_on_redis_failure(self):
        """When Redis fails, consume_nonce falls back to Postgres."""
        from wallets import consume_nonce

        mock_redis = AsyncMock()
        mock_redis.getdel = AsyncMock(side_effect=Exception("Redis error"))
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value={"nonce": "fallback-nonce"})

        with patch("wallets.get_redis", return_value=mock_redis):
            with patch.object(wallets_module, "_pool", pool):
                result = await consume_nonce("fallback-nonce")

        assert result is True
        # Postgres should have been called as fallback
        pool.fetchrow.assert_called_once()


# ─── init / close helpers ─────────────────────────────────────────────────────


@pytest.mark.anyio
class TestInitAndClose:
    async def test_close_wallets_db_clears_pool(self):
        from wallets import close_wallets_db

        with patch.object(wallets_module, "_pool", MagicMock()):
            await close_wallets_db()
        assert wallets_module._pool is None

    def test_get_pool_raises_when_uninitialised(self):
        from wallets import _get_pool

        with patch.object(wallets_module, "_pool", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                _get_pool()
