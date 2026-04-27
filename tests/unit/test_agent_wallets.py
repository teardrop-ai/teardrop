"""Unit tests for agent_wallets.py — CDP wallet provisioning with mocked DB + CDP SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent_wallets as aw_module
from agent_wallets import (
    AgentWallet,
    _chain_id_to_network,
    create_agent_wallet,
    deactivate_agent_wallet,
    get_agent_wallet,
    get_agent_wallet_balance,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
_WALLET_ID = "w-1234"
_ORG_ID = "org-abc"
_ACTOR_ID = "user-xyz"
_ADDRESS = "0x71C7656EC7ab88b098defB751B7401B5f6d8976F"


def _make_wallet(**overrides) -> dict:
    defaults = {
        "id": _WALLET_ID,
        "org_id": _ORG_ID,
        "address": _ADDRESS,
        "cdp_account_name": f"td-{_ORG_ID}",
        "chain_id": 84532,
        "wallet_type": "eoa",
        "is_active": True,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return defaults


def _mock_pool():
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()
    return pool


# ─── _chain_id_to_network ────────────────────────────────────────────────────


class TestChainIdToNetwork:
    def test_base_sepolia(self):
        assert _chain_id_to_network(84532) == "base-sepolia"

    def test_base_mainnet(self):
        assert _chain_id_to_network(8453) == "base"

    def test_unsupported_raises(self):
        with pytest.raises(ValueError, match="Unsupported chain_id 1"):
            _chain_id_to_network(1)


# ─── create_agent_wallet ─────────────────────────────────────────────────────


class TestCreateAgentWallet:
    @pytest.fixture(autouse=True)
    def _enable_cdp(self, monkeypatch):
        """Enable agent wallets in settings."""
        import config

        monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
        monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
        monkeypatch.setenv("CDP_API_KEY_SECRET", "test-secret")
        monkeypatch.setenv("CDP_WALLET_SECRET", "test-wallet-secret")
        monkeypatch.setenv("CDP_NETWORK", "base-sepolia")
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        config.get_settings.cache_clear()
        yield
        config.get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_creates_new_wallet(self):
        pool = _mock_pool()
        aw_module._pool = pool

        # First fetchrow returns None (no existing), second returns the row.
        wallet_row = _make_wallet()
        pool.fetchrow = AsyncMock(side_effect=[None, wallet_row])

        mock_account = MagicMock()
        mock_account.address = _ADDRESS

        # Mock the CDP client as an async context manager.
        mock_cdp = AsyncMock()
        mock_cdp.evm.get_or_create_account = AsyncMock(return_value=mock_account)

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_cdp)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(aw_module, "_get_cdp_client", return_value=mock_client):
            wallet = await create_agent_wallet(_ORG_ID, _ACTOR_ID)

        assert wallet.org_id == _ORG_ID
        assert wallet.address == _ADDRESS
        assert wallet.chain_id == 84532
        assert wallet.wallet_type == "eoa"
        pool.execute.assert_called()  # INSERT

    @pytest.mark.asyncio
    async def test_returns_existing_wallet_idempotent(self):
        pool = _mock_pool()
        aw_module._pool = pool

        wallet_row = _make_wallet()
        pool.fetchrow = AsyncMock(return_value=wallet_row)

        wallet = await create_agent_wallet(_ORG_ID, _ACTOR_ID)
        assert wallet.id == _WALLET_ID
        # Should NOT have called CDP SDK.
        pool.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_chain_raises(self):
        pool = _mock_pool()
        aw_module._pool = pool

        with pytest.raises(ValueError, match="Unsupported chain_id 1"):
            await create_agent_wallet(_ORG_ID, _ACTOR_ID, chain_id=1)

    @pytest.mark.asyncio
    async def test_feature_disabled_raises(self, monkeypatch):
        import config

        monkeypatch.setenv("AGENT_WALLET_ENABLED", "false")
        config.get_settings.cache_clear()

        pool = _mock_pool()
        aw_module._pool = pool

        with pytest.raises(RuntimeError, match="disabled"):
            await create_agent_wallet(_ORG_ID, _ACTOR_ID)


# ─── get_agent_wallet ────────────────────────────────────────────────────────


class TestGetAgentWallet:
    @pytest.mark.asyncio
    async def test_returns_wallet(self, monkeypatch):
        import config

        monkeypatch.setenv("CDP_NETWORK", "base-sepolia")
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        config.get_settings.cache_clear()

        pool = _mock_pool()
        aw_module._pool = pool
        pool.fetchrow = AsyncMock(return_value=_make_wallet())

        wallet = await get_agent_wallet(_ORG_ID)
        assert wallet is not None
        assert wallet.id == _WALLET_ID
        config.get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_returns_none_when_no_wallet(self, monkeypatch):
        import config

        monkeypatch.setenv("CDP_NETWORK", "base-sepolia")
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        config.get_settings.cache_clear()

        pool = _mock_pool()
        aw_module._pool = pool
        pool.fetchrow = AsyncMock(return_value=None)

        wallet = await get_agent_wallet(_ORG_ID)
        assert wallet is None
        config.get_settings.cache_clear()


# ─── get_agent_wallet_balance ─────────────────────────────────────────────────


class TestGetAgentWalletBalance:
    @pytest.fixture(autouse=True)
    def _enable_cdp(self, monkeypatch):
        import config

        monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
        monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
        monkeypatch.setenv("CDP_API_KEY_SECRET", "test-secret")
        monkeypatch.setenv("CDP_WALLET_SECRET", "test-wallet-secret")
        monkeypatch.setenv("CDP_NETWORK", "base-sepolia")
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        config.get_settings.cache_clear()
        yield
        config.get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_returns_usdc_balance(self):
        pool = _mock_pool()
        aw_module._pool = pool
        pool.fetchrow = AsyncMock(return_value=_make_wallet())

        # Mock CDP balance response.
        mock_balance = MagicMock()
        mock_balance.symbol = "USDC"
        mock_balance.amount = "10.5"

        mock_cdp = AsyncMock()
        mock_cdp.evm.list_token_balances = AsyncMock(return_value=[mock_balance])

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_cdp)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(aw_module, "_get_cdp_client", return_value=mock_client):
            result = await get_agent_wallet_balance(_ORG_ID)

        assert result["balance_usdc"] == 10_500_000
        assert result["address"] == _ADDRESS

    @pytest.mark.asyncio
    async def test_no_wallet_raises(self):
        pool = _mock_pool()
        aw_module._pool = pool
        pool.fetchrow = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="No active agent wallet"):
            await get_agent_wallet_balance(_ORG_ID)


# ─── deactivate_agent_wallet ─────────────────────────────────────────────────


class TestDeactivateAgentWallet:
    @pytest.mark.asyncio
    async def test_deactivates(self, monkeypatch):
        import config

        monkeypatch.setenv("CDP_NETWORK", "base-sepolia")
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        config.get_settings.cache_clear()

        pool = _mock_pool()
        aw_module._pool = pool
        pool.fetchrow = AsyncMock(return_value=_make_wallet())

        result = await deactivate_agent_wallet(_ORG_ID, _ACTOR_ID)
        assert result is True
        pool.execute.assert_called()
        config.get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_returns_false_if_no_wallet(self, monkeypatch):
        import config

        monkeypatch.setenv("CDP_NETWORK", "base-sepolia")
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        config.get_settings.cache_clear()

        pool = _mock_pool()
        aw_module._pool = pool
        pool.fetchrow = AsyncMock(return_value=None)

        result = await deactivate_agent_wallet(_ORG_ID, _ACTOR_ID)
        assert result is False
        config.get_settings.cache_clear()


# ─── AgentWallet model ───────────────────────────────────────────────────────


class TestAgentWalletModel:
    def test_model_roundtrip(self):
        w = AgentWallet(**_make_wallet())
        assert w.id == _WALLET_ID
        assert w.is_active is True
        assert w.wallet_type == "eoa"
        assert w.created_at == _NOW
