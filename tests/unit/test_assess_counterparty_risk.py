# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for tools/definitions/assess_counterparty_risk.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web3 import Web3

from tools._internals._debank import DebankEndpointSnapshot, DebankError, DebankWalletSnapshot
from tools.definitions.assess_counterparty_risk import (
    TOOL,
    CounterpartyRiskError,
    assess_counterparty_risk,
)

_WALLET = "0x5853ed4f26a3fcea565b3fbc698bb19cdf6deb85"
_CHECKSUMMED_WALLET = Web3.to_checksum_address(_WALLET)
_UINT256_MAX = 2**256 - 1


def _mock_w3(
    *,
    aave_account_data: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, _UINT256_MAX),
    compound_borrow: int = 0,
    compound_liquidatable: bool = False,
    aave_raises: bool = False,
    compound_raises: bool = False,
) -> MagicMock:
    mock = MagicMock()
    contract = MagicMock()

    def _aave_call(*args, **kwargs):
        m = MagicMock()
        if aave_raises:
            m.call = AsyncMock(side_effect=Exception("Aave RPC timeout"))
        else:
            m.call = AsyncMock(return_value=aave_account_data)
        return m

    def _comp_borrow(*args, **kwargs):
        m = MagicMock()
        if compound_raises:
            m.call = AsyncMock(side_effect=Exception("Compound RPC failure"))
        else:
            m.call = AsyncMock(return_value=compound_borrow)
        return m

    def _comp_liq(*args, **kwargs):
        m = MagicMock()
        if compound_raises:
            m.call = AsyncMock(side_effect=Exception("Compound RPC failure"))
        else:
            m.call = AsyncMock(return_value=compound_liquidatable)
        return m

    contract.functions.getUserAccountData.side_effect = _aave_call
    contract.functions.borrowBalanceOf.side_effect = _comp_borrow
    contract.functions.isLiquidatable.side_effect = _comp_liq
    mock.eth.contract.return_value = contract
    return mock


def _positions_snapshot(
    *,
    total_usd_value: float = 25000.0,
    errors: list[DebankError] | None = None,
) -> DebankWalletSnapshot:
    return DebankWalletSnapshot(
        total_balance={"total_usd_value": total_usd_value, "chain_list": []},
        protocol_positions=[],
        token_balances=[],
        errors=errors or [],
        source_fetched_at="2026-09-04T12:00:00Z",
        source_urls=["https://pro-openapi.debank.com/v1/user/total_balance"],
        cache_hit=False,
    )


def _history_snapshot(
    *,
    history_list: list[dict] | None = None,
    cex_dict: dict | None = None,
    error: DebankError | None = None,
) -> DebankEndpointSnapshot:
    payload = None
    if error is None:
        payload = {
            "history_list": history_list
            or [
                {
                    "sends": [{"to_addr": "0x1111111111111111111111111111111111111111"}],
                    "receives": [{"from_addr": "0x2222222222222222222222222222222222222222"}],
                    "other_addr": None,
                }
            ],
            "cex_dict": cex_dict or {"0xcex": {"name": "Binance"}},
        }
    return DebankEndpointSnapshot(
        payload=payload,
        error=error,
        source_fetched_at="2026-09-04T12:00:00Z" if error is None else None,
        source_url="https://pro-openapi.debank.com/v1/user/all_history_list",
        cache_hit=False,
    )


def _approvals_snapshot(
    *,
    tokens: list[dict] | None = None,
    error: DebankError | None = None,
) -> DebankEndpointSnapshot:
    payload = None
    if error is None:
        payload = (
            tokens
            if tokens is not None
            else [
                {
                    "id": "0xusdc",
                    "symbol": "USDC",
                    "sum_exposure_usd": 100.0,
                    "spenders": [
                        {
                            "id": "0xuniswap",
                            "value": 1000,
                            "exposure_usd": 100.0,
                            "is_contract": True,
                            "is_open_source": True,
                            "is_hacked": False,
                            "is_abandoned": False,
                        }
                    ],
                }
            ]
        )
    return DebankEndpointSnapshot(
        payload=payload,
        error=error,
        source_fetched_at="2026-09-04T12:00:00Z" if error is None else None,
        source_url="https://pro-openapi.debank.com/v1/user/token_authorized_list",
        cache_hit=False,
    )


# ─── Input Validation Tests ───────────────────────────────────────────────────


class TestInputValidation:
    @pytest.mark.anyio
    async def test_invalid_address_rejected(self):
        with pytest.raises(ValueError, match="wallet_address must be a valid 20-byte EVM address"):
            await assess_counterparty_risk(wallet_address="not-an-evm-address")

    @pytest.mark.anyio
    async def test_invalid_chain_id_rejected(self):
        with pytest.raises(ValueError, match="invalid DeBank chain identifier"):
            await assess_counterparty_risk(wallet_address=_WALLET, chain_ids=["eth", "invalid!chain"])

    @pytest.mark.anyio
    async def test_checksummed_normalization(self, monkeypatch):
        mock_w = _mock_w3()
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot()),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET.lower())
        assert res["wallet_address"] == _CHECKSUMMED_WALLET


# ─── Verdict Boundaries Tests ────────────────────────────────────────────────


class TestVerdictBoundaries:
    @pytest.mark.anyio
    async def test_verdict_acceptable_clean(self, monkeypatch):
        mock_w = _mock_w3()
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot(total_usd_value=50000.0)),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot()),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "acceptable"
        assert res["total_net_worth_usd"] == 50000.0
        assert res["data_complete"] is True
        assert res["approval_summary"]["hacked_spenders"] == 0
        assert res["liquidation"]["status"] == "no_debt"
        assert res["activity"]["tx_count"] == 1
        assert "Binance" in res["activity"]["cex_names"]

    @pytest.mark.anyio
    async def test_verdict_high_risk_hacked_spender(self, monkeypatch):
        mock_w = _mock_w3()
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        hacked_tokens = [
            {
                "id": "0xbad",
                "sum_exposure_usd": 100.0,
                "spenders": [
                    {
                        "id": "0xexploit",
                        "value": 1000,
                        "exposure_usd": 100.0,
                        "is_hacked": True,
                        "is_contract": True,
                    }
                ],
            }
        ]
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot(tokens=hacked_tokens)),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET, chain_ids=["eth"])
        assert res["verdict"] == "high_risk"
        assert res["approval_summary"]["hacked_spenders"] == 1
        assert any(rf["category"] == "approvals" and rf["severity"] == "high" for rf in res["risk_factors"])

    @pytest.mark.anyio
    async def test_verdict_high_risk_unlimited_high_exposure(self, monkeypatch):
        mock_w = _mock_w3()
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        unlimited_tokens = [
            {
                "id": "0xusdc",
                "sum_exposure_usd": 20000.0,
                "spenders": [
                    {
                        "id": "0xspender",
                        "value": str(2**256 - 1),
                        "exposure_usd": 15000.0,
                        "is_contract": True,
                        "is_open_source": True,
                    }
                ],
            }
        ]
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot(tokens=unlimited_tokens)),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "high_risk"
        assert res["approval_summary"]["unlimited"] >= 1

    @pytest.mark.anyio
    async def test_verdict_high_risk_aave_critical_hf(self, monkeypatch):
        # Aave collateral = $10,000, debt = $8,000, HF = 1.05e18 (< 1.1)
        mock_w = _mock_w3(aave_account_data=(10000_00000000, 8000_00000000, 0, 8000, 7500, int(1.05 * 1e18)))
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot()),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "high_risk"
        assert res["liquidation"]["worst_health_factor"] == 1.05

    @pytest.mark.anyio
    async def test_verdict_high_risk_compound_liquidatable(self, monkeypatch):
        mock_w = _mock_w3(compound_borrow=5000_000000, compound_liquidatable=True)
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot()),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "high_risk"
        assert res["liquidation"]["status"] == "liquidatable"

    @pytest.mark.anyio
    async def test_verdict_caution_abandoned_spender(self, monkeypatch):
        mock_w = _mock_w3()
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        abandoned_tokens = [
            {
                "id": "0xold",
                "sum_exposure_usd": 50.0,
                "spenders": [
                    {
                        "id": "0xabandoned",
                        "value": 100,
                        "exposure_usd": 50.0,
                        "is_abandoned": True,
                        "is_contract": True,
                        "is_open_source": True,
                    }
                ],
            }
        ]
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot(tokens=abandoned_tokens)),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "caution"
        assert res["approval_summary"]["abandoned_spenders"] >= 1

    @pytest.mark.anyio
    async def test_verdict_caution_unverified_spender(self, monkeypatch):
        mock_w = _mock_w3()
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        unverified_tokens = [
            {
                "id": "0xtok",
                "sum_exposure_usd": 50.0,
                "spenders": [
                    {
                        "id": "0xclosed",
                        "value": 100,
                        "exposure_usd": 50.0,
                        "is_open_source": False,
                        "is_contract": True,
                    }
                ],
            }
        ]
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot(tokens=unverified_tokens)),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "caution"
        assert res["approval_summary"]["unverified_spenders"] >= 1

    @pytest.mark.anyio
    async def test_verdict_caution_unlimited_low_exposure(self, monkeypatch):
        mock_w = _mock_w3()
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        tokens = [
            {
                "id": "0xtok",
                "sum_exposure_usd": 500.0,
                "spenders": [
                    {
                        "id": "0xspender",
                        "value": 1e50,
                        "exposure_usd": 500.0,
                        "is_contract": True,
                        "is_open_source": True,
                    }
                ],
            }
        ]
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot(tokens=tokens)),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "caution"
        assert res["approval_summary"]["unlimited"] >= 1

    @pytest.mark.anyio
    async def test_verdict_caution_aave_low_hf(self, monkeypatch):
        # Aave HF = 1.35 (< 1.5)
        mock_w = _mock_w3(aave_account_data=(10000_00000000, 6000_00000000, 0, 8000, 7500, int(1.35 * 1e18)))
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot()),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "caution"
        assert res["liquidation"]["worst_health_factor"] == 1.35


# ─── Partial and Total Failure Tests ─────────────────────────────────────────


class TestPartialAndTotalFailure:
    @pytest.mark.anyio
    async def test_partial_failure_downgrades_to_insufficient_data(self, monkeypatch):
        # Only history succeeds; positions, approvals, and liquidation fail.
        mock_w = _mock_w3(aave_raises=True, compound_raises=True)
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot(errors=[DebankError("positions", "error", "failed")])),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot()),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot(error=DebankError("approvals", "error", "failed"))),
        )

        res = await assess_counterparty_risk(wallet_address=_WALLET)
        assert res["verdict"] == "insufficient_data"
        assert res["data_complete"] is False
        assert len(res["partial_errors"]) > 0

    @pytest.mark.anyio
    async def test_total_failure_raises_counterparty_risk_error(self, monkeypatch):
        # All 4 sources fail
        mock_w = _mock_w3(aave_raises=True, compound_raises=True)
        monkeypatch.setattr("tools.definitions.assess_counterparty_risk.get_web3", lambda chain_id=1: mock_w)
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_positions",
            AsyncMock(return_value=_positions_snapshot(errors=[DebankError("positions", "error", "failed")])),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_history",
            AsyncMock(return_value=_history_snapshot(error=DebankError("history", "error", "failed"))),
        )
        monkeypatch.setattr(
            "tools.definitions.assess_counterparty_risk.fetch_wallet_approvals",
            AsyncMock(return_value=_approvals_snapshot(error=DebankError("approvals", "error", "failed"))),
        )

        with pytest.raises(CounterpartyRiskError, match="All counterparty risk data sources failed"):
            await assess_counterparty_risk(wallet_address=_WALLET)


# ─── Tool Definition Metadata Tests ──────────────────────────────────────────


class TestToolDefinitionMetadata:
    def test_tool_definition_fields(self):
        assert TOOL.name == "assess_counterparty_risk"
        assert TOOL.version == "1.0.0"
        assert "approvals" in TOOL.description.lower()
        assert TOOL.use_when != ""
        assert TOOL.limitations != ""
        assert "get_wallet_approvals" in TOOL.alternatives
        assert "get_liquidation_risk" in TOOL.alternatives
        assert "get_wallet_positions" in TOOL.alternatives
        assert "risk" in TOOL.tags
