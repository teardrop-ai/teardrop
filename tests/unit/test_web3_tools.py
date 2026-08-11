"""Unit tests for Web3 tool definitions.

All RPC calls are mocked via AsyncWeb3 patches so no live Ethereum node is needed.
Tests cover: get_eth_balance, get_erc20_balance, get_block, get_transaction, resolve_ens.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── get_eth_balance ─────────────────────────────────────────────────────────


class TestGetEthBalance:
    async def test_returns_balance_for_valid_address(self, test_settings, monkeypatch):
        from tools.definitions.get_eth_balance import get_eth_balance

        mock_w3 = MagicMock()
        mock_w3.eth.get_balance = AsyncMock(return_value=1_000_000_000_000_000_000)  # 1 ETH in wei

        monkeypatch.setattr("tools.definitions.get_eth_balance.get_web3", lambda chain_id=1: mock_w3)

        result = await get_eth_balance("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", chain_id=1)

        assert result["balance_wei"] == "1000000000000000000"
        assert result["balance_eth"] == "1"
        assert result["chain_id"] == 1

    async def test_invalid_checksum_address_raises(self, test_settings):
        from tools.definitions.get_eth_balance import get_eth_balance

        with pytest.raises(Exception):
            # All-lowercase address fails checksum
            await get_eth_balance("0xd8da6bf26964af9d7eed9e03e53415d37aa96045", chain_id=1)

    async def test_unsupported_chain_raises(self, test_settings, monkeypatch):

        monkeypatch.setattr(
            "tools.definitions.get_eth_balance.get_web3",
            lambda chain_id: (_ for _ in ()).throw(ValueError(f"Unsupported or unconfigured chain_id={chain_id}")),
        )

        with pytest.raises(Exception):
            from tools.definitions.get_eth_balance import get_eth_balance

            await get_eth_balance("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", chain_id=9999)

    async def test_base_chain_is_supported(self, test_settings, monkeypatch):
        from tools.definitions.get_eth_balance import get_eth_balance

        mock_w3 = MagicMock()
        mock_w3.eth.get_balance = AsyncMock(return_value=500_000_000_000_000_000)  # 0.5 ETH

        monkeypatch.setattr("tools.definitions.get_eth_balance.get_web3", lambda chain_id=1: mock_w3)

        result = await get_eth_balance("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", chain_id=8453)
        assert result["chain_id"] == 8453


# ─── get_erc20_balance ────────────────────────────────────────────────────────


class TestGetErc20Balance:
    async def test_returns_formatted_balance(self, test_settings, monkeypatch):
        from tools.definitions.get_erc20_balance import get_erc20_balance

        mock_w3 = MagicMock()
        monkeypatch.setattr("tools.definitions.get_erc20_balance.get_web3", lambda chain_id=1: mock_w3)

        # Patch _fetch_token_info so no real contract calls are made
        monkeypatch.setattr(
            "tools.definitions.get_erc20_balance._fetch_token_info",
            AsyncMock(return_value=(1_000_000, "USDC", 6)),
        )

        wallet = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        token = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        result = await get_erc20_balance(wallet, token, chain_id=1)

        assert result["token_symbol"] == "USDC"
        assert result["token_decimals"] == 6
        assert result["balance_raw"] == "1000000"
        assert result["chain_id"] == 1

    async def test_invalid_wallet_address_raises(self, test_settings, monkeypatch):
        from tools.definitions.get_erc20_balance import get_erc20_balance

        token = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        with pytest.raises(Exception):
            await get_erc20_balance("0xnotachecksum", token, chain_id=1)


# ─── get_block ────────────────────────────────────────────────────────────────


class TestGetBlock:
    async def test_get_latest_block(self, test_settings, monkeypatch):
        from tools.definitions.get_block import get_block

        mock_block = {
            "number": 12345,
            "hash": b"\xde\xad\xbe\xef" + b"\x00" * 28,
            "timestamp": 1700000000,
            "transactions": ["0xtx1", "0xtx2"],
            "gasUsed": 500_000,
            "gasLimit": 15_000_000,
            "baseFeePerGas": 10_000_000_000,  # 10 gwei in wei
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_block = AsyncMock(return_value=mock_block)

        monkeypatch.setattr("tools.definitions.get_block.get_web3", lambda chain_id=1: mock_w3)

        result = await get_block("latest", chain_id=1)

        assert result["number"] == 12345
        assert result["transaction_count"] == 2
        assert result["gas_used"] == 500_000

    async def test_get_block_by_number(self, test_settings, monkeypatch):
        from tools.definitions.get_block import get_block

        mock_block = {
            "number": 100,
            "hash": b"\x00" * 32,
            "timestamp": 1700000000,
            "transactions": [],
            "gasUsed": 0,
            "gasLimit": 15_000_000,
            "baseFeePerGas": None,
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_block = AsyncMock(return_value=mock_block)

        monkeypatch.setattr("tools.definitions.get_block.get_web3", lambda chain_id=1: mock_w3)

        result = await get_block("100", chain_id=1)
        assert result["number"] == 100


# ─── get_transaction ──────────────────────────────────────────────────────────


class TestGetTransaction:
    async def test_returns_transaction_details(self, test_settings, monkeypatch):
        from tools.definitions.get_transaction import get_transaction

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": 0,
            "gasPrice": 20_000_000_000,  # 20 gwei
            "input": b"\xa9\x05\x9c\xbb",
            "blockNumber": 12345,
        }
        mock_receipt = {
            "gasUsed": 21_000,
            "status": 1,
            "effectiveGasPrice": 10_000_000_000,
            "transactionIndex": 7,
            "logs": [
                {
                    "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    "topics": [b"\x01" * 32],
                    "data": b"\x12\x34",
                    "logIndex": 2,
                }
            ],
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        mock_w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)

        monkeypatch.setattr("tools.definitions.get_transaction.get_web3", lambda chain_id=1: mock_w3)

        result = await get_transaction("0xdeadbeef", chain_id=1)

        assert result["from_address"] == mock_tx["from"]
        assert result["to_address"] == mock_tx["to"]
        assert result["gas_used"] == 21_000
        assert result["status"] == 1
        assert result["block_number"] == 12345
        assert result["input_data"] == "0xa9059cbb"
        assert result["input_data_truncated"] is False
        assert result["transaction_index"] == 7
        assert result["effective_gas_price_gwei"] == "10"
        assert result["fee_wei"] == "210000000000000"
        assert result["fee_eth"] == "0.00021"
        assert result["logs"] == [
            {
                "address": mock_receipt["logs"][0]["address"],
                "topics": ["0x" + "01" * 32],
                "topics_truncated": False,
                "data": "0x1234",
                "data_truncated": False,
                "log_index": 2,
            }
        ]
        assert result["logs_truncated"] is False

    async def test_pending_tx_has_no_receipt(self, test_settings, monkeypatch):
        """Pending transactions have no receipt — status and gas_used should be None."""
        from tools.definitions.get_transaction import get_transaction

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": None,
            "value": 0,
            "gasPrice": None,
            "blockNumber": None,
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        mock_w3.eth.get_transaction_receipt = AsyncMock(side_effect=Exception("not found"))

        monkeypatch.setattr("tools.definitions.get_transaction.get_web3", lambda chain_id=1: mock_w3)

        result = await get_transaction("0xpending", chain_id=1)

        assert result["gas_used"] is None
        assert result["status"] is None
        assert result["input_data"] is None
        assert result["transaction_index"] is None
        assert result["effective_gas_price_gwei"] is None
        assert result["fee_wei"] is None
        assert result["logs"] == []

    async def test_evidence_payloads_are_bounded(self, test_settings, monkeypatch):
        from tools.definitions.get_transaction import (
            _CALLDATA_MAX_CHARS,
            _LOG_DATA_MAX_CHARS,
            _MAX_LOGS,
            get_transaction,
        )

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": 0,
            "gasPrice": None,
            "input": b"\xab\xcd" * 9_000,
            "blockNumber": 12345,
        }
        mock_receipt = {
            "gasUsed": 21_000,
            "status": 1,
            "logs": [
                {
                    "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    "topics": [b"\x00" * 32] * 6,
                    "data": b"\x12" * 5_000,
                    "logIndex": index,
                }
                for index in range(_MAX_LOGS + 1)
            ],
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        mock_w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)

        monkeypatch.setattr("tools.definitions.get_transaction.get_web3", lambda chain_id=1: mock_w3)

        result = await get_transaction("0xbounded", chain_id=1)

        assert len(result["input_data"]) <= _CALLDATA_MAX_CHARS
        assert result["input_data_truncated"] is True
        assert len(result["logs"]) == _MAX_LOGS
        assert result["logs_truncated"] is True
        assert len(result["logs"][0]["data"]) <= _LOG_DATA_MAX_CHARS
        assert result["logs"][0]["data_truncated"] is True
        assert len(result["logs"][0]["topics"]) == 4
        assert result["logs"][0]["topics_truncated"] is True

    async def test_normalizes_provider_values_and_preserves_exact_fee(self, test_settings, monkeypatch):
        from tools.definitions.get_transaction import get_transaction

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": "1000000000000000000",
            "gasPrice": "0x4a817c800",
            "input": b"\x01\x02",
            "blockNumber": "0x3039",
            "transactionIndex": "0x09",
        }
        mock_receipt = {
            "gasUsed": "1000000",
            "status": "0x1",
            "effectiveGasPrice": "100000000000",
            "logs": [],
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        mock_w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)

        monkeypatch.setattr("tools.definitions.get_transaction.get_web3", lambda chain_id=1: mock_w3)

        result = await get_transaction("0xnumeric", chain_id=1)

        assert result["value_eth"] == "1"
        assert result["gas_price_gwei"] == "20"
        assert result["gas_used"] == 1_000_000
        assert result["status"] == 1
        assert result["block_number"] == 12_345
        assert result["transaction_index"] == 9
        assert result["fee_wei"] == "100000000000000000"
        assert result["fee_eth"] == "0.1"
        json.dumps(result)

    async def test_malformed_hex_values_are_safe_empty_payloads(self, test_settings, monkeypatch):
        from tools.definitions.get_transaction import get_transaction

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": None,
            "value": 0,
            "gasPrice": None,
            "input": object(),
            "blockNumber": None,
        }
        mock_receipt = {
            "gasUsed": None,
            "status": 0,
            "logs": [{"address": object(), "topics": [object()], "data": object()}],
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        mock_w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)

        monkeypatch.setattr("tools.definitions.get_transaction.get_web3", lambda chain_id=1: mock_w3)

        result = await get_transaction("0xmalformed", chain_id=1)

        assert result["input_data"] == "0x"
        assert result["logs"][0]["address"] == "0x"
        assert result["logs"][0]["topics"] == ["0x"]
        assert result["logs"][0]["data"] == "0x"
        json.dumps(result)


# ─── resolve_ens ─────────────────────────────────────────────────────────────


class TestResolveEns:
    async def test_forward_lookup_returns_checksummed_address(self, test_settings, monkeypatch):
        from tools.definitions.resolve_ens import resolve_ens

        mock_w3 = MagicMock()
        mock_w3.ens.address = AsyncMock(return_value="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
        mock_w3.ens.get_text = AsyncMock(return_value=None)

        monkeypatch.setattr("tools.definitions.resolve_ens.get_web3", lambda chain_id=1: mock_w3)

        result = await resolve_ens("vitalik.eth")

        assert result["resolved"] is True
        assert result["address"] == "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        assert result["name"] == "vitalik.eth"
        assert result["error"] is None

    async def test_forward_lookup_populates_avatar(self, test_settings, monkeypatch):
        from tools.definitions.resolve_ens import resolve_ens

        mock_w3 = MagicMock()
        mock_w3.ens.address = AsyncMock(return_value="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
        mock_w3.ens.get_text = AsyncMock(return_value="https://example.com/avatar.png")

        monkeypatch.setattr("tools.definitions.resolve_ens.get_web3", lambda chain_id=1: mock_w3)

        result = await resolve_ens("vitalik.eth")

        assert result["avatar"] == "https://example.com/avatar.png"

    async def test_rpc_error_populates_error_field(self, test_settings, monkeypatch):
        from tools.definitions.resolve_ens import resolve_ens

        mock_w3 = MagicMock()
        mock_w3.ens.address = AsyncMock(side_effect=Exception("RPC timeout"))

        monkeypatch.setattr("tools.definitions.resolve_ens.get_web3", lambda chain_id=1: mock_w3)

        result = await resolve_ens("doesnotexist123456789.eth")

        assert result["resolved"] is False
        assert result["address"] is None
        # Error should be surfaced, not swallowed.
        assert result["error"] is not None
        assert "RPC timeout" in result["error"]

    async def test_unresolvable_name_returns_resolved_false(self, test_settings, monkeypatch):
        from tools.definitions.resolve_ens import resolve_ens

        mock_w3 = MagicMock()
        mock_w3.ens.address = AsyncMock(return_value=None)

        monkeypatch.setattr("tools.definitions.resolve_ens.get_web3", lambda chain_id=1: mock_w3)

        result = await resolve_ens("doesnotexist123456789.eth")

        assert result["resolved"] is False
        assert result["address"] is None
        assert result["error"] is None

    async def test_reverse_lookup_address_returns_ens_name(self, test_settings, monkeypatch):
        from tools.definitions.resolve_ens import resolve_ens

        mock_w3 = MagicMock()
        mock_w3.ens.name = AsyncMock(return_value="vitalik.eth")

        monkeypatch.setattr("tools.definitions.resolve_ens.get_web3", lambda chain_id=1: mock_w3)

        result = await resolve_ens("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")

        assert result["resolved"] is True
        assert result["name"] == "vitalik.eth"
        assert result["address"] == "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

    async def test_reverse_lookup_no_primary_name(self, test_settings, monkeypatch):
        from tools.definitions.resolve_ens import resolve_ens

        mock_w3 = MagicMock()
        mock_w3.ens.name = AsyncMock(return_value=None)

        monkeypatch.setattr("tools.definitions.resolve_ens.get_web3", lambda chain_id=1: mock_w3)

        result = await resolve_ens("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")

        assert result["resolved"] is False
        assert result["name"] is None

    def test_invalid_input_raises_validation_error(self):
        from pydantic import ValidationError

        from tools.definitions.resolve_ens import ResolveEnsInput

        with pytest.raises(ValidationError):
            ResolveEnsInput(name="not_valid_ens_or_address")

    def test_empty_input_raises_validation_error(self):
        from pydantic import ValidationError

        from tools.definitions.resolve_ens import ResolveEnsInput

        with pytest.raises(ValidationError):
            ResolveEnsInput(name="   ")
