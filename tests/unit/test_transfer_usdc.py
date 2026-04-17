"""Unit tests for agent_wallets.transfer_usdc()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent_wallets as aw_module
import config


@pytest.fixture(autouse=True)
def _enable_cdp(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-key-secret")
    monkeypatch.setenv("CDP_WALLET_SECRET", "test-wallet-secret")
    monkeypatch.setenv("CDP_NETWORK", "base-sepolia")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


_VALID_ADDR = "0x1234567890123456789012345678901234567890"


@pytest.mark.asyncio
async def test_transfer_usdc_success():
    mock_result = MagicMock()
    mock_result.transaction_hash = "0xdeadbeef"

    mock_cdp = AsyncMock()
    mock_cdp.evm.transfer = AsyncMock(return_value=mock_result)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_cdp)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch.object(aw_module, "_get_cdp_client", return_value=mock_client):
        tx_hash = await aw_module.transfer_usdc("td-marketplace", _VALID_ADDR, 1_000_000)

    assert tx_hash == "0xdeadbeef"
    mock_cdp.evm.transfer.assert_called_once_with(
        from_account="td-marketplace",
        to=_VALID_ADDR,
        token="usdc",
        amount="1",
        network="base-sepolia",
    )


@pytest.mark.asyncio
async def test_transfer_usdc_invalid_address():
    with pytest.raises(ValueError, match="Invalid destination address"):
        await aw_module.transfer_usdc("td-marketplace", "not-an-address", 1_000_000)


@pytest.mark.asyncio
async def test_transfer_usdc_zero_amount():
    with pytest.raises(ValueError, match="positive"):
        await aw_module.transfer_usdc("td-marketplace", _VALID_ADDR, 0)


@pytest.mark.asyncio
async def test_transfer_usdc_cdp_failure():
    mock_cdp = AsyncMock()
    mock_cdp.evm.transfer = AsyncMock(side_effect=Exception("CDP API down"))

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_cdp)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch.object(aw_module, "_get_cdp_client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="CDP transfer failed"):
            await aw_module.transfer_usdc("td-marketplace", _VALID_ADDR, 1_000_000)
