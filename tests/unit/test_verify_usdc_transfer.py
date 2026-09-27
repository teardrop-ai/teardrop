# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for agent_wallets.verify_usdc_transfer and
get_settlement_wallet_balance_usdc — all network calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from teardrop.agent_wallets import check_usdc_transfer, get_settlement_wallet_balance_usdc, verify_usdc_transfer

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
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: _mock_settings())
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
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: _mock_settings())
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
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: _mock_settings(timeout=1))
        # Freeze time so the deadline is immediately exceeded after the first poll.
        call_count = 0

        def _fast_time():
            nonlocal call_count
            call_count += 1
            # First call returns a base time; all subsequent calls return base + 2
            # (past the 1s deadline) so the loop exits quickly.
            return 0.0 if call_count == 1 else 2.0

        monkeypatch.setattr("teardrop.agent_wallets.time.monotonic", _fast_time)
        monkeypatch.setattr("teardrop.agent_wallets.asyncio.sleep", AsyncMock())

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
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: s)

        with pytest.raises(ValueError, match="No RPC URL available"):
            # chain_id=1 has no fallback in _FALLBACK_RPC
            await verify_usdc_transfer(_TX_HASH, chain_id=1, timeout_seconds=5)

    @pytest.mark.anyio
    async def test_uses_fallback_rpc_when_no_base_rpc_url(self, monkeypatch):
        """Falls back to public RPC when base_rpc_url is empty but chain is known."""
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: _mock_settings(base_rpc_url=""))
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
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: _mock_settings(timeout=30))
        monkeypatch.setattr("teardrop.agent_wallets.asyncio.sleep", AsyncMock())

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
    async def test_http_error_retries(self, monkeypatch, caplog):
        """HTTPError on first poll → retries and succeeds on second."""
        caplog.set_level("DEBUG", logger="teardrop.agent_wallets")
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: _mock_settings(timeout=30))
        monkeypatch.setattr("teardrop.agent_wallets.asyncio.sleep", AsyncMock())

        call_count = 0

        def _side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("https://rpc.example/secret-key")
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
        assert "secret-key" not in caplog.text
        assert "error_type=ConnectError" in caplog.text


# ─── get_settlement_wallet_balance_usdc ──────────────────────────────────────


class TestGetSettlementWalletBalanceUsdc:
    @pytest.mark.anyio
    async def test_returns_usdc_balance(self, monkeypatch):
        """Returns atomic USDC balance from CDP SDK."""
        s = _mock_settings()
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: s)

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

        with patch("teardrop.agent_wallets._get_cdp_client", return_value=mock_cdp):
            balance = await get_settlement_wallet_balance_usdc(chain_id=84532)

        assert balance == 5_000_000

    @pytest.mark.anyio
    async def test_returns_zero_when_no_usdc_token(self, monkeypatch):
        """Returns 0 if the CDP account holds no USDC."""
        s = _mock_settings()
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: s)

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

        with patch("teardrop.agent_wallets._get_cdp_client", return_value=mock_cdp):
            balance = await get_settlement_wallet_balance_usdc(chain_id=84532)

        assert balance == 0

    @pytest.mark.anyio
    async def test_raises_when_cdp_disabled(self, monkeypatch):
        """RuntimeError if AGENT_WALLET_ENABLED=false."""
        s = _mock_settings()
        s.agent_wallet_enabled = False
        monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: s)

        with pytest.raises(RuntimeError, match="disabled"):
            await get_settlement_wallet_balance_usdc(chain_id=84532)


# ─── check_usdc_transfer ─────────────────────────────────────────────────────

_USDC_SEPOLIA = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_RECIPIENT = "0x1234567890123456789012345678901234567890"
_AMOUNT = 1_500_000
_BLOCK_HASH = "0x" + "ab" * 32


def _topic(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def _transfer_log(*, contract: str = _USDC_SEPOLIA, to: str = _RECIPIENT, amount: int = _AMOUNT) -> dict:
    return {
        "address": contract,
        "topics": [_TRANSFER_TOPIC, _topic("0x" + "11" * 20), _topic(to)],
        "data": "0x" + format(amount, "064x"),
    }


def _receipt(status: str = "0x1", logs: list | None = None, block_number: str = "0x10") -> dict:
    return {
        "transactionHash": _TX_HASH,
        "status": status,
        "blockNumber": block_number,
        "blockHash": _BLOCK_HASH,
        "logs": [_transfer_log()] if logs is None else logs,
    }


def _transfer_tx(*, to: str = _USDC_SEPOLIA, recipient: str = _RECIPIENT, amount: int = _AMOUNT) -> dict:
    return {"to": to, "input": "0xa9059cbb" + recipient[2:].lower().rjust(64, "0") + format(amount, "064x")}


def _rpc_client(receipt, *, finalized_number: str = "0x20", block_hash: str = _BLOCK_HASH, tx=None):
    def _post(_url, json):
        method, params = json["method"], json["params"]
        if method == "eth_getTransactionReceipt":
            result = receipt
        elif method == "eth_getBlockByNumber" and params[0] == "finalized":
            result = {"number": finalized_number}
        elif method == "eth_getBlockByNumber":
            result = {"number": params[0], "hash": block_hash}
        elif method == "eth_getTransactionByHash":
            result = tx
        else:
            raise AssertionError(f"unexpected RPC method {method}")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"jsonrpc": "2.0", "id": 1, "result": result})
        return resp

    client = AsyncMock()
    client.post = AsyncMock(side_effect=_post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def _check(monkeypatch, client, *, tx_hash: str = _TX_HASH, chain_id: int = 84532):
    monkeypatch.setattr("teardrop.agent_wallets.get_settings", lambda: _mock_settings())
    with patch("httpx.AsyncClient", return_value=client):
        return await check_usdc_transfer(tx_hash, to_address=_RECIPIENT, amount_usdc=_AMOUNT, chain_id=chain_id)


class TestCheckUsdcTransfer:
    @pytest.mark.anyio
    async def test_finalized_matching_transfer_is_confirmed(self, monkeypatch):
        assert await _check(monkeypatch, _rpc_client(_receipt())) == "confirmed"

    @pytest.mark.anyio
    async def test_missing_receipt_is_pending(self, monkeypatch):
        assert await _check(monkeypatch, _rpc_client(None)) == "pending"

    @pytest.mark.anyio
    async def test_block_above_finalized_head_is_pending(self, monkeypatch):
        client = _rpc_client(_receipt(block_number="0x30"), finalized_number="0x20")
        assert await _check(monkeypatch, client) == "pending"

    @pytest.mark.anyio
    async def test_non_canonical_block_hash_is_pending(self, monkeypatch):
        client = _rpc_client(_receipt(), block_hash="0x" + "cd" * 32)
        assert await _check(monkeypatch, client) == "pending"

    @pytest.mark.anyio
    async def test_pending_receipt_on_revert_is_not_acted_on_before_finality(self, monkeypatch):
        client = _rpc_client(_receipt(status="0x0", logs=[], block_number="0x30"), tx=_transfer_tx())
        assert await _check(monkeypatch, client) == "pending"

    @pytest.mark.anyio
    async def test_finalized_revert_of_matching_transfer_call_is_reverted(self, monkeypatch):
        client = _rpc_client(_receipt(status="0x0", logs=[]), tx=_transfer_tx())
        assert await _check(monkeypatch, client) == "reverted"

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "tx",
        [
            None,
            _transfer_tx(to="0x" + "22" * 20),
            _transfer_tx(recipient="0x" + "33" * 20),
            _transfer_tx(amount=_AMOUNT + 1),
        ],
    )
    async def test_revert_of_unrelated_tx_is_mismatch(self, monkeypatch, tx):
        client = _rpc_client(_receipt(status="0x0", logs=[]), tx=tx)
        assert await _check(monkeypatch, client) == "mismatch"

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "logs",
        [
            [],
            [_transfer_log(contract="0x" + "44" * 20)],
            [_transfer_log(to="0x" + "55" * 20)],
            [_transfer_log(amount=_AMOUNT - 1)],
        ],
    )
    async def test_success_without_exact_usdc_payment_is_mismatch(self, monkeypatch, logs):
        assert await _check(monkeypatch, _rpc_client(_receipt(logs=logs))) == "mismatch"

    @pytest.mark.anyio
    async def test_malformed_tx_hash_is_mismatch_without_rpc(self, monkeypatch):
        client = _rpc_client(_receipt())
        assert await _check(monkeypatch, client, tx_hash="cdp-transfer-object") == "mismatch"
        client.post.assert_not_called()

    @pytest.mark.anyio
    async def test_rpc_error_raises(self, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}})
        client = AsyncMock()
        client.post = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(RuntimeError, match="eth_getTransactionReceipt"):
            await _check(monkeypatch, client)

    @pytest.mark.anyio
    async def test_unknown_chain_raises(self, monkeypatch):
        with pytest.raises(ValueError, match="No USDC contract"):
            await _check(monkeypatch, _rpc_client(_receipt()), chain_id=1)
