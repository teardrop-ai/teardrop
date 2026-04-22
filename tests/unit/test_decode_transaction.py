"""Unit tests for tools/definitions/decode_transaction.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDecodeTransaction:
    async def test_eth_transfer_no_calldata(self, test_settings, monkeypatch):
        from tools.definitions.decode_transaction import decode_transaction

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": 1_000_000_000_000_000_000,  # 1 ETH
            "input": b"",
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)

        monkeypatch.setattr(
            "tools.definitions.decode_transaction.get_web3", lambda chain_id=1: mock_w3
        )

        result = await decode_transaction("0xdeadbeef", chain_id=1)

        assert result["function_name"] == "transfer (native ETH)"
        assert result["value_eth"] == "1"
        assert result["decoded_args"] is None

    async def test_decode_with_abi(self, test_settings, monkeypatch):
        from tools.definitions.decode_transaction import decode_transaction

        # ERC-20 transfer selector: a9059cbb
        selector = bytes.fromhex("a9059cbb")
        # Pad address and amount as ABI-encoded args
        addr_arg = bytes(12) + bytes.fromhex("A0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
        amount_arg = (1000000).to_bytes(32, "big")
        calldata = selector + addr_arg + amount_arg

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": 0,
            "input": calldata,
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)

        # Mock contract decode
        mock_contract = MagicMock()
        mock_fn = MagicMock()
        mock_fn.fn_name = "transfer"
        mock_contract.decode_function_input.return_value = (
            mock_fn,
            {"_to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "_value": 1000000},
        )
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr(
            "tools.definitions.decode_transaction.get_web3", lambda chain_id=1: mock_w3
        )

        abi = json.dumps([{
            "type": "function",
            "name": "transfer",
            "inputs": [
                {"name": "_to", "type": "address"},
                {"name": "_value", "type": "uint256"},
            ],
            "outputs": [{"name": "", "type": "bool"}],
            "stateMutability": "nonpayable",
        }])

        result = await decode_transaction("0xdeadbeef", chain_id=1, abi_json=abi)

        assert result["function_name"] == "transfer"
        assert result["decode_source"] == "provided_abi"
        assert result["decoded_args"]["_to"] == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

    async def test_fallback_to_4byte(self, test_settings, monkeypatch):
        from tools.definitions.decode_transaction import decode_transaction

        selector = bytes.fromhex("a9059cbb")
        calldata = selector + b"\x00" * 64

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": 0,
            "input": calldata,
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)

        monkeypatch.setattr(
            "tools.definitions.decode_transaction.get_web3", lambda chain_id=1: mock_w3
        )

        # Mock 4byte.directory response
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "results": [{"text_signature": "transfer(address,uint256)"}]
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.decode_transaction.aiohttp.ClientSession", return_value=mock_session):  # noqa: E501
            result = await decode_transaction("0xdeadbeef", chain_id=1)

        assert result["function_name"] == "transfer(address,uint256)"
        assert result["decode_source"] == "4byte.directory"

    def test_invalid_tx_hash_raises_validation_error(self):
        from pydantic import ValidationError
        from tools.definitions.decode_transaction import DecodeTransactionInput

        with pytest.raises(ValidationError):
            DecodeTransactionInput(tx_hash="0xdeadbeef")  # too short

    def test_tx_hash_normalised_to_lowercase(self):
        from tools.definitions.decode_transaction import DecodeTransactionInput

        valid_hash = "0x" + "A" * 64
        inp = DecodeTransactionInput(tx_hash=valid_hash)
        assert inp.tx_hash == valid_hash.lower()

    async def test_receipt_status_and_gas_used_populated(self, test_settings, monkeypatch):
        from tools.definitions.decode_transaction import decode_transaction

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": 0,
            "input": b"",
            "blockNumber": 19_000_000,
        }
        mock_receipt = {
            "status": 1,
            "gasUsed": 21_000,
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        mock_w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)

        monkeypatch.setattr(
            "tools.definitions.decode_transaction.get_web3", lambda chain_id=1: mock_w3
        )

        valid_hash = "0x" + "b" * 64
        result = await decode_transaction(valid_hash, chain_id=1)

        assert result["status"] == 1
        assert result["gas_used"] == 21_000
        assert result["block_number"] == 19_000_000

    async def test_pending_transaction_has_none_status(self, test_settings, monkeypatch):
        from tools.definitions.decode_transaction import decode_transaction

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": 0,
            "input": b"",
            "blockNumber": None,
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        # Pending txs have no receipt — raise to simulate node returning None / error.
        mock_w3.eth.get_transaction_receipt = AsyncMock(side_effect=Exception("not found"))

        monkeypatch.setattr(
            "tools.definitions.decode_transaction.get_web3", lambda chain_id=1: mock_w3
        )

        valid_hash = "0x" + "c" * 64
        result = await decode_transaction(valid_hash, chain_id=1)

        assert result["status"] is None
        assert result["gas_used"] is None

    async def test_calldata_truncated_at_limit(self, test_settings, monkeypatch):
        from tools.definitions.decode_transaction import _CALLDATA_MAX_CHARS, decode_transaction

        # Generate calldata longer than the truncation limit.
        big_input = b"\xab\xcd" * (_CALLDATA_MAX_CHARS + 100)

        mock_tx = {
            "from": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "value": 0,
            "input": big_input,
            "blockNumber": 1,
        }
        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        mock_w3.eth.get_transaction_receipt = AsyncMock(
            return_value={"status": 1, "gasUsed": 100_000}
        )

        monkeypatch.setattr(
            "tools.definitions.decode_transaction.get_web3", lambda chain_id=1: mock_w3
        )

        valid_hash = "0x" + "d" * 64
        result = await decode_transaction(valid_hash, chain_id=1)

        assert len(result["raw_calldata"]) <= _CALLDATA_MAX_CHARS

    def test_oversized_abi_raises_validation_error(self):
        from pydantic import ValidationError
        from tools.definitions.decode_transaction import DecodeTransactionInput

        valid_hash = "0x" + "e" * 64
        with pytest.raises(ValidationError):
            DecodeTransactionInput(tx_hash=valid_hash, abi_json="x" * 65_537)
