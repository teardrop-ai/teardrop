"""Unit tests for tools/definitions/read_contract.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestReadContract:
    @pytest.fixture
    def view_abi(self):
        return json.dumps(
            [
                {
                    "type": "function",
                    "name": "totalSupply",
                    "inputs": [],
                    "outputs": [{"name": "", "type": "uint256"}],
                    "stateMutability": "view",
                }
            ]
        )

    @pytest.fixture
    def payable_abi(self):
        return json.dumps(
            [
                {
                    "type": "function",
                    "name": "deposit",
                    "inputs": [],
                    "outputs": [],
                    "stateMutability": "payable",
                }
            ]
        )

    async def test_view_function_succeeds(self, test_settings, monkeypatch, view_abi):
        from tools.definitions.read_contract import read_contract

        mock_fn = MagicMock()
        mock_fn.return_value.call = AsyncMock(return_value=1_000_000_000)

        mock_contract = MagicMock()
        mock_contract.functions.__getitem__ = MagicMock(return_value=mock_fn)

        mock_w3 = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.read_contract.get_web3", lambda chain_id=1: mock_w3)

        result = await read_contract(
            contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            abi_fragment=view_abi,
            function_name="totalSupply",
        )

        assert result["function_name"] == "totalSupply"
        assert result["result"] == 1_000_000_000

    async def test_payable_function_rejected(self, test_settings, payable_abi):
        from tools.definitions.read_contract import read_contract

        with pytest.raises(ValueError, match="stateMutability"):
            await read_contract(
                contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                abi_fragment=payable_abi,
                function_name="deposit",
            )

    async def test_nonpayable_function_rejected(self, test_settings):
        from tools.definitions.read_contract import read_contract

        abi = json.dumps(
            [
                {
                    "type": "function",
                    "name": "transfer",
                    "inputs": [
                        {"name": "_to", "type": "address"},
                        {"name": "_value", "type": "uint256"},
                    ],
                    "outputs": [{"name": "", "type": "bool"}],
                    "stateMutability": "nonpayable",
                }
            ]
        )

        with pytest.raises(ValueError, match="stateMutability"):
            await read_contract(
                contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                abi_fragment=abi,
                function_name="transfer",
            )

    async def test_invalid_json_raises(self, test_settings):
        from tools.definitions.read_contract import read_contract

        with pytest.raises(ValueError, match="Invalid ABI JSON"):
            await read_contract(
                contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                abi_fragment="not json",
                function_name="foo",
            )

    async def test_function_not_in_abi_raises(self, test_settings, view_abi):
        from tools.definitions.read_contract import read_contract

        with pytest.raises(ValueError, match="not found"):
            await read_contract(
                contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                abi_fragment=view_abi,
                function_name="nonexistent",
            )

    async def test_bytes_return_serialized_to_hex(self, test_settings, monkeypatch, view_abi):
        from tools.definitions.read_contract import read_contract

        mock_fn = MagicMock()
        mock_fn.return_value.call = AsyncMock(return_value=b"\xde\xad\xbe\xef")

        mock_contract = MagicMock()
        mock_contract.functions.__getitem__ = MagicMock(return_value=mock_fn)

        mock_w3 = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.read_contract.get_web3", lambda chain_id=1: mock_w3)

        result = await read_contract(
            contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            abi_fragment=view_abi,
            function_name="totalSupply",
        )

        assert result["result"] == "0xdeadbeef"

    async def test_pure_function_allowed(self, test_settings, monkeypatch):
        from tools.definitions.read_contract import read_contract

        pure_abi = json.dumps(
            [
                {
                    "type": "function",
                    "name": "add",
                    "inputs": [
                        {"name": "a", "type": "uint256"},
                        {"name": "b", "type": "uint256"},
                    ],
                    "outputs": [{"name": "", "type": "uint256"}],
                    "stateMutability": "pure",
                }
            ]
        )

        mock_fn = MagicMock()
        mock_fn.return_value.call = AsyncMock(return_value=42)

        mock_contract = MagicMock()
        mock_contract.functions.__getitem__ = MagicMock(return_value=mock_fn)

        mock_w3 = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.read_contract.get_web3", lambda chain_id=1: mock_w3)

        result = await read_contract(
            contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            abi_fragment=pure_abi,
            function_name="add",
            args=[20, 22],
        )

        assert result["result"] == 42

    async def test_struct_result_serialized_as_dict(self, test_settings, monkeypatch, view_abi):
        """web3 AttributeDict / struct returns must be serialized as plain dicts."""
        from web3.datastructures import AttributeDict

        from tools.definitions.read_contract import read_contract

        struct_return = AttributeDict({"amount": 1000, "recipient": "0xabc"})

        mock_fn = MagicMock()
        mock_fn.return_value.call = AsyncMock(return_value=struct_return)

        mock_contract = MagicMock()
        mock_contract.functions.__getitem__ = MagicMock(return_value=mock_fn)

        mock_w3 = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.read_contract.get_web3", lambda chain_id=1: mock_w3)

        result = await read_contract(
            contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            abi_fragment=view_abi,
            function_name="totalSupply",
        )

        assert isinstance(result["result"], dict)
        assert result["result"]["amount"] == 1000

    async def test_block_identifier_passed_to_call(self, test_settings, monkeypatch, view_abi):
        from tools.definitions.read_contract import read_contract

        captured_kwargs: dict = {}

        async def fake_call(**kwargs):
            captured_kwargs.update(kwargs)
            return 42

        mock_fn = MagicMock()
        mock_fn.return_value.call = fake_call

        mock_contract = MagicMock()
        mock_contract.functions.__getitem__ = MagicMock(return_value=mock_fn)

        mock_w3 = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.read_contract.get_web3", lambda chain_id=1: mock_w3)

        await read_contract(
            contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            abi_fragment=view_abi,
            function_name="totalSupply",
            block_identifier="19000000",
        )

        assert captured_kwargs.get("block_identifier") == 19_000_000

    def test_oversized_abi_raises_validation_error(self):
        from pydantic import ValidationError

        from tools.definitions.read_contract import ReadContractInput

        huge_abi = "x" * 65_537
        with pytest.raises(ValidationError):
            ReadContractInput(
                contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                abi_fragment=huge_abi,
                function_name="totalSupply",
            )


class TestReadContractSchema:
    def test_args_schema_uses_concrete_item_type_for_gemini(self):
        from tools.definitions.read_contract import ReadContractInput

        schema = ReadContractInput.model_json_schema()
        args_schema = schema["properties"]["args"]
        assert args_schema.get("type") == "array"
        items = args_schema.get("items", {})
        assert items.get("type") == "string"
