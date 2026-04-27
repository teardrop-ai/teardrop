"""Unit tests for agent_wallets.verify_usdc_transfer and
get_settlement_wallet_balance_usdc — all network calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_wallets import get_settlement_wallet_balance_usdc, verify_usdc_transfer

_TX_HASH = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_RPC_URL = "https://sepolia.base.org"


def _mock_settings(base_rpc_url: str = _RPC_URL, timeout: int = 10, chain_id: int = 84532):
    s = MagicMock()
    s.base_rpc_url = base_rpc_url
    s.marketplace_tx_confirm_timeout_seconds = timeout
    s.marketplace_settlement_chain_id = chain_id
    s.marketplace_settlement_cdp_account = "td-marketplace"
    s.cdp_network = "base-sepolia"
    s.agent_wallet_enabled = True
    s.cdp_configured = True
    return s


def _rpc_receipt_response(status: str = "0x1") -> dict:
    """Minimal eth_getTransactionReceipt response with given status."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "transactionHash": _TX_HASH,
            "status": status,
            "blockNumber": "0x1a",
        },
    }


def _rpc_pending_response() -> dict:
    """RPC response when tx is not yet mined."""
    return {"jsonrpc": "2.0", "id": 1, "result": None}


# ─── verify_usdc_transfer ─────────────────────────────────────────────────────


class TestVerifyUsdcTransfer:
    @pytest.mark.anyio
    async def test_confirmed_success_returns_true(self, monkeypatch):
        """Mined tx with status 0x1 → True."""
        monkeypatch.setattr("agent_wallets.get_settings", lambda: _mock_settings())
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=_rpc_receipt_response("0x1"))

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await verify_usdc_transfer(_TX_HASH, chain_id=84532, timeout_seconds=5)

        assert result is True

    @pytest.mark.anyio
    async def test_reverted_tx_returns_false(self, monkeypatch):
        """Mined tx with status 0x0 → False."""
        monkeypatch.setattr("agent_wallets.get_settings", lambda: _mock_settings())
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=_rpc_receipt_response("0x0"))

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await verify_usdc_transfer(_TX_HASH, chain_id=84532, timeout_seconds=5)

        assert result is False

    @pytest.mark.anyio
    async def test_timeout_raises_timeout_error(self, monkeypatch):
        """No receipt within timeout → TimeoutError."""
        monkeypatch.setattr("agent_wallets.get_settings", lambda: _mock_settings(timeout=1))
        # Freeze time so the deadline is immediately exceeded after the first poll.
        call_count = 0

        def _fast_time():
            nonlocal call_count
            call_count += 1
            # First call returns a base time; all subsequent calls return base + 2
            # (past the 1s deadline) so the loop exits quickly.
            return 0.0 if call_count == 1 else 2.0

        monkeypatch.setattr("agent_wallets.time.monotonic", _fast_time)
        monkeypatch.setattr("agent_wallets.asyncio.sleep", AsyncMock())

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=_rpc_pending_response())

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(TimeoutError):
                await verify_usdc_transfer(_TX_HASH, chain_id=84532, timeout_seconds=1)

    @pytest.mark.anyio
    async def test_no_rpc_url_raises_value_error(self, monkeypatch):
        """No base_rpc_url and unsupported chain → ValueError."""
        s = _mock_settings(base_rpc_url="")
        s.cdp_network = "base-sepolia"
        monkeypatch.setattr("agent_wallets.get_settings", lambda: s)

        with pytest.raises(ValueError, match="No RPC URL available"):
            # chain_id=1 has no fallback in _FALLBACK_RPC
            await verify_usdc_transfer(_TX_HASH, chain_id=1, timeout_seconds=5)

    @pytest.mark.anyio
    async def test_uses_fallback_rpc_when_no_base_rpc_url(self, monkeypatch):
        """Falls back to public RPC when base_rpc_url is empty but chain is known."""
        monkeypatch.setattr("agent_wallets.get_settings", lambda: _mock_settings(base_rpc_url=""))
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=_rpc_receipt_response("0x1"))

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await verify_usdc_transfer(_TX_HASH, chain_id=84532, timeout_seconds=5)

        assert result is True
        # Confirm it called the public fallback URL, not an empty string.
        call_args = mock_client.post.call_args
        assert "sepolia.base.org" in call_args[0][0]

    @pytest.mark.anyio
    async def test_polls_until_receipt_appears(self, monkeypatch):
        """Returns True after receiving None then a mined receipt."""
        monkeypatch.setattr("agent_wallets.get_settings", lambda: _mock_settings(timeout=30))
        monkeypatch.setattr("agent_wallets.asyncio.sleep", AsyncMock())

        responses = [_rpc_pending_response(), _rpc_pending_response(), _rpc_receipt_response("0x1")]
        resp_iter = iter(responses)

        def _next_resp(*_args, **_kwargs):
            data = next(resp_iter)
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value=data)
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=_next_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await verify_usdc_transfer(_TX_HASH, chain_id=84532, timeout_seconds=30)

        assert result is True
        assert mock_client.post.call_count == 3

    @pytest.mark.anyio
    async def test_http_error_retries(self, monkeypatch):
        """HTTPError on first poll → retries and succeeds on second."""
        monkeypatch.setattr("agent_wallets.get_settings", lambda: _mock_settings(timeout=30))
        monkeypatch.setattr("agent_wallets.asyncio.sleep", AsyncMock())

        call_count = 0

        def _side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("connection refused")
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value=_rpc_receipt_response("0x1"))
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=_side_effect)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await verify_usdc_transfer(_TX_HASH, chain_id=84532, timeout_seconds=30)

        assert result is True
        assert call_count == 2


# ─── get_settlement_wallet_balance_usdc ──────────────────────────────────────


class TestGetSettlementWalletBalanceUsdc:
    @pytest.mark.anyio
    async def test_returns_usdc_balance(self, monkeypatch):
        """Returns atomic USDC balance from CDP SDK."""
        s = _mock_settings()
        monkeypatch.setattr("agent_wallets.get_settings", lambda: s)

        usdc_token = MagicMock()
        usdc_token.symbol = "USDC"
        usdc_token.amount = "5.0"  # $5.00 = 5_000_000 atomic

        mock_account = MagicMock()
        mock_account.address = "0x1234"

        mock_cdp = AsyncMock()
        mock_cdp.evm.get_or_create_account = AsyncMock(return_value=mock_account)
        mock_cdp.evm.list_token_balances = AsyncMock(return_value=[usdc_token])
        mock_cdp.__aenter__ = AsyncMock(return_value=mock_cdp)
        mock_cdp.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_wallets._get_cdp_client", return_value=mock_cdp):
            balance = await get_settlement_wallet_balance_usdc(chain_id=84532)

        assert balance == 5_000_000

    @pytest.mark.anyio
    async def test_returns_zero_when_no_usdc_token(self, monkeypatch):
        """Returns 0 if the CDP account holds no USDC."""
        s = _mock_settings()
        monkeypatch.setattr("agent_wallets.get_settings", lambda: s)

        eth_token = MagicMock()
        eth_token.symbol = "ETH"
        eth_token.amount = "1.0"

        mock_account = MagicMock()
        mock_account.address = "0x1234"

        mock_cdp = AsyncMock()
        mock_cdp.evm.get_or_create_account = AsyncMock(return_value=mock_account)
        mock_cdp.evm.list_token_balances = AsyncMock(return_value=[eth_token])
        mock_cdp.__aenter__ = AsyncMock(return_value=mock_cdp)
        mock_cdp.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_wallets._get_cdp_client", return_value=mock_cdp):
            balance = await get_settlement_wallet_balance_usdc(chain_id=84532)

        assert balance == 0

    @pytest.mark.anyio
    async def test_raises_when_cdp_disabled(self, monkeypatch):
        """RuntimeError if AGENT_WALLET_ENABLED=false."""
        s = _mock_settings()
        s.agent_wallet_enabled = False
        monkeypatch.setattr("agent_wallets.get_settings", lambda: s)

        with pytest.raises(RuntimeError, match="disabled"):
            await get_settlement_wallet_balance_usdc(chain_id=84532)
