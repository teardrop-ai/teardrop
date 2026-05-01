# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for tools/definitions/get_liquidation_risk.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

_WALLET_A = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # vitalik.eth
_WALLET_B = "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8"  # binance7
_WALLET_C = "0x28C6c06298d514Db089934071355E5743bf21d60"  # binance14

_UINT256_MAX = 2**256 - 1


def _build_mock_w3(
    *,
    aave_by_wallet: dict[str, tuple[int, int, int, int, int, int]] | None = None,
    aave_default: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, _UINT256_MAX),
    aave_raise_on: set[str] | None = None,
    compound_borrow_by_wallet: dict[str, int] | None = None,
    compound_liquidatable_by_wallet: dict[str, bool] | None = None,
    compound_borrow_default: int = 0,
    compound_liquidatable_default: bool = False,
    compound_raise_wallets: set[str] | None = None,
    block_number: int = 12345,
    block_raises: bool = False,
) -> MagicMock:
    """Build a mock AsyncWeb3 whose contract.functions.X(wallet).call() return
    per-wallet data. Supports simulating protocol-level RPC failures.
    """
    aave_by_wallet = aave_by_wallet or {}
    compound_borrow_by_wallet = compound_borrow_by_wallet or {}
    compound_liquidatable_by_wallet = compound_liquidatable_by_wallet or {}
    compound_raise_wallets = compound_raise_wallets or set()
    aave_raise_on = aave_raise_on or set()

    mock_w3 = MagicMock()
    mock_contract = MagicMock()

    def _aave_account_call(wallet_arg):
        m = MagicMock()
        if wallet_arg in aave_raise_on:
            m.call = AsyncMock(side_effect=Exception("simulated aave failure"))
        else:
            m.call = AsyncMock(return_value=aave_by_wallet.get(wallet_arg, aave_default))
        return m

    mock_contract.functions.getUserAccountData.side_effect = _aave_account_call

    def _borrow_call(wallet_arg):
        m = MagicMock()
        if wallet_arg in compound_raise_wallets:
            m.call = AsyncMock(side_effect=Exception("simulated compound failure"))
        else:
            m.call = AsyncMock(return_value=compound_borrow_by_wallet.get(wallet_arg, compound_borrow_default))
        return m

    mock_contract.functions.borrowBalanceOf.side_effect = _borrow_call

    def _liq_call(wallet_arg):
        m = MagicMock()
        if wallet_arg in compound_raise_wallets:
            m.call = AsyncMock(side_effect=Exception("simulated compound failure"))
        else:
            m.call = AsyncMock(return_value=compound_liquidatable_by_wallet.get(wallet_arg, compound_liquidatable_default))
        return m

    mock_contract.functions.isLiquidatable.side_effect = _liq_call

    mock_w3.eth.contract.return_value = mock_contract

    async def _bn():
        if block_raises:
            raise Exception("block_number failure")
        return block_number

    mock_w3.eth.block_number = _bn()

    return mock_w3


def _patch(monkeypatch, mock_w3):
    monkeypatch.setattr("tools.definitions.get_liquidation_risk.get_web3", lambda chain_id=1: mock_w3)


# ─── Input validation ────────────────────────────────────────────────────────


class TestInputValidation:
    async def test_unsupported_chain_raises(self, test_settings):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        with pytest.raises(ValueError, match="chain_id"):
            await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=137)

    async def test_empty_list_raises(self, test_settings):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        with pytest.raises(ValueError, match="empty"):
            await get_liquidation_risk(wallet_addresses=[], chain_id=1)

    async def test_over_cap_raises(self, test_settings):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        # 51 valid addresses (each an EIP-55 checksummed distinct wallet)
        wallets = [
            "0x" + f"{i:040x}"[:40].replace("0", "0")  # zero-pad hex; any 0x-prefixed hex works
            for i in range(1, 52)
        ]
        # Use Web3 to checksum so validator accepts shapes
        from web3 import Web3

        wallets = [Web3.to_checksum_address(w) for w in wallets]
        with pytest.raises(ValueError, match="50"):
            await get_liquidation_risk(wallet_addresses=wallets, chain_id=1)

    async def test_malformed_address_rejected(self, test_settings):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        with pytest.raises(Exception):  # noqa: BLE001
            await get_liquidation_risk(wallet_addresses=["not-an-address"], chain_id=1)

    async def test_duplicates_deduped_silently(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        mock_w3 = _build_mock_w3()
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A, _WALLET_A, _WALLET_B], chain_id=1)

        # Only 2 unique wallets should be in results
        assert len(result["results"]) == 2
        addrs = [r["wallet_address"] for r in result["results"]]
        # Order preserved (first-occurrence)
        assert addrs == [_WALLET_A, _WALLET_B]

    async def test_pydantic_input_validates_cap(self):
        from web3 import Web3

        from tools.definitions.get_liquidation_risk import GetLiquidationRiskInput

        wallets = [Web3.to_checksum_address("0x" + f"{i:040x}"[:40]) for i in range(1, 52)]
        with pytest.raises(ValueError):
            GetLiquidationRiskInput(wallet_addresses=wallets)

    async def test_uses_global_rpc_semaphore(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        call_count = 0

        class _DummySem:
            async def __aenter__(self):
                nonlocal call_count
                call_count += 1
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        mock_w3 = _build_mock_w3()
        _patch(monkeypatch, mock_w3)
        monkeypatch.setattr("tools.definitions.get_liquidation_risk.acquire_rpc_semaphore", lambda: _DummySem())

        await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)
        assert call_count == 1


# ─── Aave tier classification ────────────────────────────────────────────────


class TestAaveTiers:
    """Verify tier boundaries at HF = 1.0, 1.05, 1.15, 1.5."""

    @pytest.mark.parametrize(
        "hf_float,expected_tier",
        [
            (0.5, "liquidatable"),
            (0.999, "liquidatable"),
            (1.0, "critical"),  # boundary: hf >= 1.0 && < 1.05
            (1.049, "critical"),
            (1.05, "warning"),  # boundary
            (1.149, "warning"),
            (1.15, "caution"),  # boundary
            (1.499, "caution"),
            (1.5, "healthy"),  # boundary
            (2.5, "healthy"),
        ],
    )
    async def test_tier_boundaries(self, test_settings, monkeypatch, hf_float, expected_tier):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        # Debt non-zero so "no_debt" path is avoided
        raw_hf = int(hf_float * 10**18)
        aave_data = (10000 * 10**8, 5000 * 10**8, 0, 8500, 8000, raw_hf)
        mock_w3 = _build_mock_w3(aave_by_wallet={_WALLET_A: aave_data})
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)

        assert result["results"][0]["aave"]["risk_tier"] == expected_tier

    async def test_no_debt_via_zero(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        # total_debt_base=0 → no_debt regardless of raw HF
        aave_data = (20000 * 10**8, 0, 15000 * 10**8, 8500, 8000, int(2.5 * 10**18))
        mock_w3 = _build_mock_w3(aave_by_wallet={_WALLET_A: aave_data})
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)

        aave = result["results"][0]["aave"]
        assert aave["risk_tier"] == "no_debt"
        assert aave["health_factor"] is None
        assert aave["total_collateral_usd"] == 20000.0

    async def test_no_debt_via_uint256_max(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        # Aave signals "no debt" via raw HF = type(uint256).max
        aave_data = (20000 * 10**8, 0, 0, 8500, 8000, _UINT256_MAX)
        mock_w3 = _build_mock_w3(aave_by_wallet={_WALLET_A: aave_data})
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)
        assert result["results"][0]["aave"]["risk_tier"] == "no_debt"
        assert result["results"][0]["aave"]["health_factor"] is None


# ─── Compound risk ───────────────────────────────────────────────────────────


class TestCompound:
    async def test_no_debt_omits_market(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        # borrow=0 and not liquidatable → market skipped
        mock_w3 = _build_mock_w3()
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)

        assert result["results"][0]["compound"] == []

    async def test_borrowing_tier(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        mock_w3 = _build_mock_w3(
            compound_borrow_by_wallet={_WALLET_A: 500 * 10**6},
            compound_liquidatable_by_wallet={_WALLET_A: False},
        )
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)

        markets = result["results"][0]["compound"]
        assert len(markets) == 3  # All 3 Ethereum Compound markets return same mock
        for m in markets:
            assert m["risk_tier"] == "borrowing"
            assert m["is_liquidatable"] is False
            assert m["borrow_balance_raw"] == str(500 * 10**6)

    async def test_liquidatable_tier(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        mock_w3 = _build_mock_w3(
            compound_borrow_by_wallet={_WALLET_A: 1000 * 10**6},
            compound_liquidatable_by_wallet={_WALLET_A: True},
        )
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)

        markets = result["results"][0]["compound"]
        for m in markets:
            assert m["risk_tier"] == "liquidatable"
            assert m["is_liquidatable"] is True


# ─── overall_tier aggregation ────────────────────────────────────────────────


class TestOverallTier:
    async def test_overall_takes_worst(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        # Aave healthy (HF=2.0) but Compound liquidatable → overall = liquidatable
        aave_data = (10000 * 10**8, 1000 * 10**8, 0, 8500, 8000, int(2.0 * 10**18))
        mock_w3 = _build_mock_w3(
            aave_by_wallet={_WALLET_A: aave_data},
            compound_borrow_by_wallet={_WALLET_A: 100 * 10**6},
            compound_liquidatable_by_wallet={_WALLET_A: True},
        )
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)
        assert result["results"][0]["overall_tier"] == "liquidatable"

    async def test_overall_no_debt_when_all_empty(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        mock_w3 = _build_mock_w3()
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)
        assert result["results"][0]["overall_tier"] == "no_debt"


# ─── Per-protocol failure isolation ──────────────────────────────────────────


class TestFailureIsolation:
    async def test_aave_failure_does_not_block_compound(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        mock_w3 = _build_mock_w3(
            aave_raise_on={_WALLET_A},
            compound_borrow_by_wallet={_WALLET_A: 500 * 10**6},
            compound_liquidatable_by_wallet={_WALLET_A: False},
        )
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)

        r = result["results"][0]
        assert r["aave"] is None
        assert len(r["compound"]) == 3
        error_protocols = {e["protocol"] for e in r["errors"]}
        assert "aave_v3" in error_protocols
        # overall_tier reflects compound borrowing state
        assert r["overall_tier"] == "borrowing"

    async def test_compound_failure_does_not_block_aave(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        aave_data = (10000 * 10**8, 5000 * 10**8, 0, 8500, 8000, int(2.0 * 10**18))
        mock_w3 = _build_mock_w3(
            aave_by_wallet={_WALLET_A: aave_data},
            compound_raise_wallets={_WALLET_A},
        )
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)

        r = result["results"][0]
        assert r["aave"] is not None
        assert r["aave"]["risk_tier"] == "healthy"
        # Inner Compound failures are caught per-market and return None, so the
        # outer try/except does not fire — markets are simply absent.
        assert r["compound"] == []

    async def test_one_wallet_failure_does_not_block_others(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        aave_healthy = (10000 * 10**8, 1000 * 10**8, 0, 8500, 8000, int(3.0 * 10**18))
        mock_w3 = _build_mock_w3(
            aave_by_wallet={_WALLET_B: aave_healthy},
            aave_raise_on={_WALLET_A},
        )
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A, _WALLET_B], chain_id=1)

        assert len(result["results"]) == 2
        # Wallet A errored out on Aave
        a = next(r for r in result["results"] if r["wallet_address"] == _WALLET_A)
        assert a["aave"] is None
        assert "aave_v3" in {e["protocol"] for e in a["errors"]}
        # Wallet B fetched successfully
        b = next(r for r in result["results"] if r["wallet_address"] == _WALLET_B)
        assert b["aave"] is not None
        assert b["aave"]["risk_tier"] == "healthy"


# ─── Batch summary ───────────────────────────────────────────────────────────


class TestSummary:
    async def test_counts_by_tier(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        # A = liquidatable, B = healthy, C = no_debt
        mock_w3 = _build_mock_w3(
            aave_by_wallet={
                _WALLET_A: (10000 * 10**8, 9000 * 10**8, 0, 8500, 8000, int(0.9 * 10**18)),
                _WALLET_B: (10000 * 10**8, 1000 * 10**8, 0, 8500, 8000, int(3.0 * 10**18)),
                _WALLET_C: (5000 * 10**8, 0, 0, 8500, 8000, _UINT256_MAX),
            },
        )
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A, _WALLET_B, _WALLET_C], chain_id=1)

        s = result["summary"]
        assert s["total_wallets"] == 3
        assert s["liquidatable_count"] == 1
        assert s["healthy_count"] == 1
        assert s["no_debt_count"] == 1
        assert s["critical_count"] == 0


# ─── Output schema / tool registration ───────────────────────────────────────


class TestOutputSchema:
    async def test_block_number_and_note_present(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        mock_w3 = _build_mock_w3(block_number=98765)
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)

        assert result["data_block_number"] == 98765
        assert result["chain_id"] == 1
        assert "snapshot" in result["note"].lower()

    async def test_block_number_failure_graceful(self, test_settings, monkeypatch):
        from tools.definitions.get_liquidation_risk import get_liquidation_risk

        mock_w3 = _build_mock_w3(block_raises=True)
        _patch(monkeypatch, mock_w3)

        result = await get_liquidation_risk(wallet_addresses=[_WALLET_A], chain_id=1)
        assert result["data_block_number"] == 0

    async def test_tool_registered(self):
        from tools.definitions.get_liquidation_risk import TOOL

        assert TOOL.name == "get_liquidation_risk"
        assert TOOL.version == "1.0.0"
        assert "liquidation" in TOOL.tags
        assert "aave" in TOOL.tags
        assert "compound" in TOOL.tags

    async def test_tool_registered_in_package(self):
        from tools.definitions import _ALL_TOOLS

        names = {t.name for t in _ALL_TOOLS}
        assert "get_liquidation_risk" in names
