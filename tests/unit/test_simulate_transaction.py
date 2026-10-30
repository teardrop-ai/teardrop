# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for tools/definitions/simulate_transaction.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import encode as abi_encode
from pydantic import ValidationError

OWNER = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
TOKEN_IN = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
TOKEN_OUT = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
NFT = "0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
APPROVE_ABI = json.dumps(
    [
        {
            "type": "function",
            "name": "approve",
            "stateMutability": "nonpayable",
            "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
            "outputs": [{"name": "", "type": "bool"}],
        }
    ]
)


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower()[2:]


def _word(value: int) -> str:
    return "0x" + format(value, "064x")


def _log(emitter: str, topic0: str, first: str, second: str, amount: int) -> dict[str, Any]:
    return {"address": emitter.lower(), "topics": [topic0, _topic(first), _topic(second)], "data": _word(amount)}


def _revert_data(reason: str) -> str:
    return "0x08c379a0" + abi_encode(["string"], [reason]).hex()


def _mock_web3(monkeypatch, handler):
    calls: list[tuple[str, list[Any]]] = []

    async def make_request(method, params):
        calls.append((method, params))
        return handler(method, params)

    w3 = MagicMock()
    w3.provider.make_request = AsyncMock(side_effect=make_request)
    monkeypatch.setattr("tools.definitions.simulate_transaction.get_web3", lambda chain_id=1: w3)
    return calls


def _bundle_handler(call_results: list[dict[str, Any]]):
    def handler(method, params):
        if method == "eth_blockNumber":
            return {"jsonrpc": "2.0", "id": 1, "result": "0x10"}
        if method == "eth_simulateV1":
            return {"jsonrpc": "2.0", "id": 1, "result": [{"number": "0x11", "calls": call_results}]}
        raise AssertionError(f"unexpected method {method}")

    return handler


def _two_calls() -> list[dict[str, Any]]:
    return [
        {"to": TOKEN_IN, "abi_fragment": APPROVE_ABI, "function_name": "approve", "args_json": json.dumps([ROUTER, "1000"])},
        {"to": ROUTER, "data": "0xabcdef01", "value_wei": "5"},
    ]


class TestBundleSimulation:
    async def test_bundle_reports_outcomes_balance_changes_and_approvals(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        unlimited = 2**256 - 1
        results = [
            {
                "status": "0x1",
                "gasUsed": "0xb411",
                "returnData": _word(1),
                "logs": [_log(TOKEN_IN, APPROVAL, OWNER, ROUTER, unlimited)],
            },
            {
                "status": "0x1",
                "gasUsed": "0x2710",
                "returnData": "0x",
                "logs": [
                    _log(TOKEN_IN, TRANSFER, OWNER, ROUTER, 1_000),
                    _log(TOKEN_OUT, TRANSFER, ROUTER, OWNER, 400),
                    _log(NATIVE, TRANSFER, OWNER, ROUTER, 5),
                    _log(TOKEN_IN, TRANSFER, ROUTER, NFT, 999),
                    {**_log(NFT, TRANSFER, OWNER, ROUTER, 0), "topics": [TRANSFER, _topic(OWNER), _topic(ROUTER), _word(7)]},
                ],
            },
        ]
        calls = _mock_web3(monkeypatch, _bundle_handler(results))

        result = await simulate_transaction(from_address=OWNER.lower(), calls=_two_calls())

        assert result["mode"] == "eth_simulateV1"
        assert result["base_block_number"] == 16
        assert result["all_succeeded"] is True
        assert [call["gas_used"] for call in result["calls"]] == [0xB411, 0x2710]
        assert {change["token"]: change["delta_raw"] for change in result["balance_changes"]} == {
            "ETH": "-5",
            TOKEN_IN: "-1000",
            TOKEN_OUT: "400",
        }
        assert result["approvals"] == [{"token": TOKEN_IN, "spender": ROUTER, "amount_raw": str(unlimited), "unlimited": True}]
        assert result["receipt"]["version"] == 2
        assert result["receipt"]["kind"] == "simulation"
        assert len(result["receipt"]["content_sha256"]) == 64

        method, params = calls[1]
        assert method == "eth_simulateV1"
        payload, block = params
        assert block == "0x10"
        assert payload["traceTransfers"] is True and payload["validation"] is False
        sent = payload["blockStateCalls"][0]["calls"]
        assert sent[0]["input"].startswith("0x095ea7b3")
        assert sent[0]["input"].endswith(format(1000, "064x"))
        assert "value" not in sent[0]
        assert sent[1] == {"from": OWNER, "to": ROUTER, "input": "0xabcdef01", "value": "0x5"}

    async def test_reverted_call_decodes_reason(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        results = [
            {"status": "0x1", "gasUsed": "0x1", "returnData": "0x", "logs": []},
            {
                "status": "0x0",
                "gasUsed": "0x5208",
                "returnData": "0x",
                "error": {"code": 3, "message": "execution reverted", "data": _revert_data("STF")},
            },
        ]
        _mock_web3(monkeypatch, _bundle_handler(results))

        result = await simulate_transaction(from_address=OWNER, calls=_two_calls())

        assert result["all_succeeded"] is False
        reverted = result["calls"][1]
        assert reverted["status"] == "reverted"
        assert reverted["revert_reason"] == "STF"
        assert reverted["gas_used"] == 0x5208

    async def test_explicit_block_skips_block_number_lookup(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        calls = _mock_web3(monkeypatch, _bundle_handler([{"status": "0x1", "gasUsed": "0x1", "returnData": "0x"}]))

        result = await simulate_transaction(
            from_address=OWNER, calls=[{"to": ROUTER, "data": "0x"}], chain_id=8453, block_identifier="123"
        )

        assert result["base_block_number"] == 123
        assert [method for method, _ in calls] == ["eth_simulateV1"]
        assert calls[0][1][1] == "0x7b"

    async def test_long_return_data_is_truncated_but_hashed_in_full(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        def run(return_data: str) -> Any:
            _mock_web3(monkeypatch, _bundle_handler([{"status": "0x1", "gasUsed": "0x1", "returnData": return_data}]))
            return simulate_transaction(from_address=OWNER, calls=[{"to": ROUTER, "data": "0x"}], block_identifier="1")

        long_a = "0x" + "ab" * 4_000
        long_b = long_a[:-2] + "cd"
        first = await run(long_a)
        second = await run(long_b)

        assert first["calls"][0]["return_data_truncated"] is True
        assert first["calls"][0]["return_data"] == second["calls"][0]["return_data"]
        assert first["receipt"]["content_sha256"] != second["receipt"]["content_sha256"]

    async def test_receipt_is_deterministic_and_binds_block(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        _mock_web3(monkeypatch, _bundle_handler([{"status": "0x1", "gasUsed": "0x1", "returnData": "0x"}]))
        call = [{"to": ROUTER, "data": "0x01"}]
        first = await simulate_transaction(from_address=OWNER, calls=call, block_identifier="7")
        again = await simulate_transaction(from_address=OWNER, calls=call, block_identifier="7")
        other_block = await simulate_transaction(from_address=OWNER, calls=call, block_identifier="8")

        assert first["receipt"] == again["receipt"]
        assert first["receipt"]["content_sha256"] != other_block["receipt"]["content_sha256"]

    async def test_accepts_validated_model_instances(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import SimulationCall, simulate_transaction

        _mock_web3(monkeypatch, _bundle_handler([{"status": "0x1", "gasUsed": "0x1", "returnData": "0x"}]))

        result = await simulate_transaction(
            from_address=OWNER, calls=[SimulationCall(to=ROUTER, data="0x")], block_identifier="1"
        )

        assert result["all_succeeded"] is True

    async def test_reported_changes_are_capped(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        tokens = ["0x" + format(index + 1, "040x") for index in range(60)]
        logs = [_log(token, TRANSFER, ROUTER, OWNER, 1) for token in tokens]
        _mock_web3(monkeypatch, _bundle_handler([{"status": "0x1", "gasUsed": "0x1", "returnData": "0x", "logs": logs}]))

        result = await simulate_transaction(from_address=OWNER, calls=[{"to": ROUTER, "data": "0x"}], block_identifier="1")

        assert len(result["balance_changes"]) == 50
        assert any("truncated" in warning for warning in result["warnings"])

    async def test_malformed_response_raises(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        _mock_web3(monkeypatch, _bundle_handler([]))

        with pytest.raises(ValueError, match="different number"):
            await simulate_transaction(from_address=OWNER, calls=[{"to": ROUTER, "data": "0x"}], block_identifier="1")


class TestFallback:
    async def test_unsupported_method_falls_back_to_independent_calls(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        def handler(method, params):
            if method == "eth_simulateV1":
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32601, "message": "the method eth_simulateV1 does not exist"},
                }
            tx = params[0]
            assert params[1] == "0x9"
            if tx["to"] == ROUTER:
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": 3, "message": "execution reverted", "data": _revert_data("no")},
                }
            if method == "eth_call":
                return {"jsonrpc": "2.0", "id": 1, "result": _word(1)}
            return {"jsonrpc": "2.0", "id": 1, "result": "0xb411"}

        calls = _mock_web3(monkeypatch, handler)

        result = await simulate_transaction(from_address=OWNER, calls=_two_calls(), block_identifier="9")

        assert result["mode"] == "eth_call_fallback"
        assert result["balance_changes"] == [] and result["approvals"] == []
        assert any("independently" in warning for warning in result["warnings"])
        assert result["calls"][0]["status"] == "success"
        assert result["calls"][0]["gas_used"] == 0xB411
        assert result["calls"][1]["status"] == "reverted"
        assert result["calls"][1]["revert_reason"] == "no"
        eth_calls = [params[0] for method, params in calls if method == "eth_call"]
        assert {tx["to"] for tx in eth_calls} == {TOKEN_IN, ROUTER}
        assert all("data" in tx and "input" not in tx for tx in eth_calls)

    async def test_unsupported_method_exception_text_also_falls_back(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        w3 = MagicMock()

        async def make_request(method, params):
            if method == "eth_simulateV1":
                raise RuntimeError("Unsupported method: eth_simulateV1")
            return {"jsonrpc": "2.0", "id": 1, "result": "0x"}

        w3.provider.make_request = AsyncMock(side_effect=make_request)
        monkeypatch.setattr("tools.definitions.simulate_transaction.get_web3", lambda chain_id=1: w3)

        result = await simulate_transaction(from_address=OWNER, calls=[{"to": ROUTER, "data": "0x"}], block_identifier="1")

        assert result["mode"] == "eth_call_fallback"

    async def test_other_simulation_errors_are_not_masked(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        def handler(method, params):
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -38014, "message": "gas limit exceeded block cap"}}

        _mock_web3(monkeypatch, handler)

        with pytest.raises(ValueError, match="eth_simulateV1 failed"):
            await simulate_transaction(from_address=OWNER, calls=[{"to": ROUTER, "data": "0x"}], block_identifier="1")


class TestDecodeRevertReason:
    def test_error_panic_custom_and_empty(self):
        from tools.definitions.simulate_transaction import decode_revert_reason

        assert decode_revert_reason(_revert_data("Too little received")) == "Too little received"
        assert decode_revert_reason("0x4e487b71" + abi_encode(["uint256"], [0x11]).hex()) == "Panic(0x11)"
        assert decode_revert_reason("0xdeadbeef") == "custom error 0xdeadbeef"
        assert decode_revert_reason("0x08c379a0zz") is None
        assert decode_revert_reason("0x") is None
        assert decode_revert_reason(None) is None


class TestInputValidation:
    @pytest.mark.parametrize(
        "call",
        [
            {"to": ROUTER, "data": "0x01", "abi_fragment": APPROVE_ABI, "function_name": "approve"},
            {"to": ROUTER, "abi_fragment": APPROVE_ABI},
            {"to": ROUTER, "data": "0x1"},
            {"to": ROUTER, "data": "0x", "value_wei": "-1"},
            {"to": ROUTER, "data": "0x", "value_wei": str(2**256)},
            {"to": "not-an-address", "data": "0x"},
        ],
    )
    def test_rejects_invalid_calls(self, call):
        from tools.definitions.simulate_transaction import SimulateTransactionInput

        with pytest.raises(ValidationError):
            SimulateTransactionInput(from_address=OWNER, calls=[call])

    @pytest.mark.parametrize(
        "overrides",
        [
            {"calls": []},
            {"calls": [{"to": ROUTER, "data": "0x"}] * 9},
            {"chain_id": 137},
            {"block_identifier": "pending"},
            {"from_address": "0x123"},
        ],
    )
    def test_rejects_invalid_request(self, overrides):
        from tools.definitions.simulate_transaction import SimulateTransactionInput

        values: dict[str, Any] = {"from_address": OWNER, "calls": [{"to": ROUTER, "data": "0x"}], **overrides}
        with pytest.raises(ValidationError):
            SimulateTransactionInput(**values)

    async def test_unencodable_abi_arguments_raise_value_error(self, test_settings, monkeypatch):
        from tools.definitions.simulate_transaction import simulate_transaction

        _mock_web3(monkeypatch, _bundle_handler([]))
        bad = {"to": TOKEN_IN, "abi_fragment": APPROVE_ABI, "function_name": "approve", "args_json": '["x"]'}

        with pytest.raises(ValueError, match="Could not ABI-encode"):
            await simulate_transaction(from_address=OWNER, calls=[bad])


def test_tool_is_registered_read_only():
    from tools.definitions import _ALL_TOOLS

    tool = next(tool for tool in _ALL_TOOLS if tool.name == "simulate_transaction")
    assert tool.annotations == {"readOnlyHint": True}
