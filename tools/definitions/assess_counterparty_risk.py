# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Composite counterparty risk assessment backed by DeBank Cloud and on-chain RPC."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from tools._internals._debank import (
    DebankEndpointSnapshot,
    DebankWalletSnapshot,
)
from tools._internals._debank import (
    get_wallet_approvals as fetch_wallet_approvals,
)
from tools._internals._debank import (
    get_wallet_history as fetch_wallet_history,
)
from tools._internals._debank import (
    get_wallet_positions as fetch_wallet_positions,
)
from tools._internals._web3_helpers import get_web3, rpc_call
from tools._internals.provenance import DataProvenance, cache_age_seconds, utc_now_iso
from tools.definitions.get_defi_positions import (
    _AAVE_V3_POOL,
    _AAVE_V3_POOL_ABI,
    _COMET_ABI,
    _COMPOUND_V3_MARKETS,
    _HEALTH_FACTOR_INFINITE_THRESHOLD,
    _UINT256_MAX,
)
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_CHAIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CHAIN_NAME_TO_EVM: dict[str, int] = {
    "eth": 1,
    "ethereum": 1,
    "base": 8453,
}
_UNLIMITED_THRESHOLD: float = 2**128


class CounterpartyRiskError(RuntimeError):
    """Raised when all counterparty risk data sources fail."""


def _is_unlimited(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        val = float(value)
        return val >= _UNLIMITED_THRESHOLD or val >= 1e38
    except (TypeError, ValueError):
        return False


async def _rpc_call_with_chain(coro_fn, chain_id: int):
    """Call rpc_call with chain_id, falling back for stubs without chain_id."""
    try:
        return await rpc_call(coro_fn, chain_id=chain_id)
    except TypeError as exc:
        if "unexpected keyword argument 'chain_id'" in str(exc):
            return await rpc_call(coro_fn)
        raise


# ─── Schemas ─────────────────────────────────────────────────────────────────


class AssessCounterpartyRiskInput(BaseModel):
    wallet_address: str = Field(..., description="EVM wallet address (0x-prefixed)")
    chain_ids: list[str] = Field(
        default_factory=lambda: ["eth", "base"],
        description="DeBank chain identifiers scoping approvals and history; positions and net worth remain all-chain.",
    )

    @field_validator("wallet_address")
    @classmethod
    def _validate_wallet_address(cls, value: str) -> str:
        try:
            return Web3.to_checksum_address(value.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("wallet_address must be a valid 20-byte EVM address") from exc

    @field_validator("chain_ids", mode="before")
    @classmethod
    def _validate_chain_ids(cls, value: Any) -> list[str]:
        if value is None:
            return ["eth", "base"]
        if not isinstance(value, list) or not value:
            raise ValueError("chain_ids must be a non-empty list when provided")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("chain_ids must contain valid DeBank chain identifiers")
            cid = item.strip().lower()
            if not _CHAIN_ID_RE.fullmatch(cid):
                raise ValueError(f"chain_ids contains an invalid DeBank chain identifier: {cid}")
            if cid not in normalized:
                normalized.append(cid)
        return normalized or ["eth", "base"]


class RiskFactor(BaseModel):
    category: str
    severity: Literal["high", "medium", "low"]
    detail: str


class ApprovalSummary(BaseModel):
    total: int = 0
    unlimited: int = 0
    sum_exposure_usd: float = 0.0
    hacked_spenders: int = 0
    abandoned_spenders: int = 0
    unverified_spenders: int = 0


class LiquidationSummary(BaseModel):
    status: str = "no_debt"
    worst_health_factor: float | None = None
    has_borrow_positions: bool = False


class ActivitySummary(BaseModel):
    tx_count: int = 0
    distinct_counterparties: int = 0
    cex_names: list[str] = Field(default_factory=list)


class AssessCounterpartyRiskOutput(BaseModel):
    wallet_address: str
    verdict: Literal["high_risk", "caution", "acceptable", "insufficient_data"]
    risk_factors: list[RiskFactor] = Field(default_factory=list)
    approval_summary: ApprovalSummary = Field(default_factory=ApprovalSummary)
    liquidation: LiquidationSummary = Field(default_factory=LiquidationSummary)
    activity: ActivitySummary = Field(default_factory=ActivitySummary)
    total_net_worth_usd: float | None = None
    partial_errors: list[str] = Field(default_factory=list)
    data_complete: bool = True
    provenance: DataProvenance
    as_of: str


# ─── Liquidation Assessment ──────────────────────────────────────────────────


async def _assess_liquidation(
    wallet: str,
    evm_chain_ids: list[int],
) -> tuple[LiquidationSummary, list[RiskFactor], list[str]]:
    """Scan Aave v3 and Compound v3 for liquidation and debt health."""
    summary = LiquidationSummary()
    risk_factors: list[RiskFactor] = []
    errors: list[str] = []
    worst_hf: float | None = None
    worst_status = "no_debt"
    has_debt = False

    for chain_id in evm_chain_ids:
        # Aave v3 scan
        if chain_id in _AAVE_V3_POOL:
            try:
                w3 = get_web3(chain_id)
                pool_addr = Web3.to_checksum_address(_AAVE_V3_POOL[chain_id])
                pool = w3.eth.contract(address=pool_addr, abi=_AAVE_V3_POOL_ABI)
                account_data = await _rpc_call_with_chain(
                    lambda: pool.functions.getUserAccountData(wallet).call(),
                    chain_id,
                )
                (
                    total_collateral_base,
                    total_debt_base,
                    _avail_borrows_base,
                    _liq_thresh,
                    _ltv,
                    health_factor_raw,
                ) = account_data
                total_debt_int = int(total_debt_base)
                hf_raw_int = int(health_factor_raw)

                if total_debt_int > 0 and hf_raw_int < _HEALTH_FACTOR_INFINITE_THRESHOLD and hf_raw_int != _UINT256_MAX:
                    has_debt = True
                    hf = hf_raw_int / 1e18
                    if worst_hf is None or hf < worst_hf:
                        worst_hf = hf
                    if hf < 1.0:
                        worst_status = "liquidatable"
                        risk_factors.append(
                            RiskFactor(
                                category="liquidation",
                                severity="high",
                                detail=f"Aave v3 position is liquidatable (health factor: {hf:.2f}) on chain {chain_id}",
                            )
                        )
                    elif hf < 1.1:
                        if worst_status != "liquidatable":
                            worst_status = "critical"
                        risk_factors.append(
                            RiskFactor(
                                category="liquidation",
                                severity="high",
                                detail=f"Aave v3 position is near liquidation (health factor: {hf:.2f}) on chain {chain_id}",
                            )
                        )
                    elif hf < 1.5:
                        if worst_status not in ("liquidatable", "critical"):
                            worst_status = "caution"
                        risk_factors.append(
                            RiskFactor(
                                category="liquidation",
                                severity="medium",
                                detail=f"Aave v3 position has low health factor ({hf:.2f}) on chain {chain_id}",
                            )
                        )
                    elif worst_status == "no_debt":
                        worst_status = "healthy"
            except Exception as exc:
                logger.debug("Aave v3 liquidation scan error on chain %s: %s", chain_id, exc)
                errors.append(f"Aave v3 (chain {chain_id}): {exc}")

        # Compound v3 scan
        markets = _COMPOUND_V3_MARKETS.get(chain_id, [])
        for market in markets:
            try:
                w3 = get_web3(chain_id)
                market_addr = Web3.to_checksum_address(market["address"])
                comet = w3.eth.contract(address=market_addr, abi=_COMET_ABI)
                borrowed, is_liq = await asyncio.gather(
                    _rpc_call_with_chain(lambda: comet.functions.borrowBalanceOf(wallet).call(), chain_id),
                    _rpc_call_with_chain(lambda: comet.functions.isLiquidatable(wallet).call(), chain_id),
                )
                borrowed_int = int(borrowed)
                if borrowed_int > 0 or bool(is_liq):
                    has_debt = True
                    if bool(is_liq):
                        worst_status = "liquidatable"
                        risk_factors.append(
                            RiskFactor(
                                category="liquidation",
                                severity="high",
                                detail=f"Compound v3 {market.get('name', 'market')} is liquidatable on chain {chain_id}",
                            )
                        )
                    elif borrowed_int > 0:
                        if worst_status in ("no_debt", "healthy"):
                            worst_status = "borrowing"
                        m_name = market.get("name", "market")
                        risk_factors.append(
                            RiskFactor(
                                category="liquidation",
                                severity="medium",
                                detail=f"Compound v3 {m_name} has active borrow balance on chain {chain_id}",
                            )
                        )
            except Exception as exc:
                logger.debug("Compound v3 market %s scan error: %s", market.get("name"), exc)
                errors.append(f"Compound v3 {market.get('name')} (chain {chain_id}): {exc}")

    summary.status = worst_status
    summary.worst_health_factor = round(worst_hf, 4) if worst_hf is not None else None
    summary.has_borrow_positions = has_debt
    return summary, risk_factors, errors


# ─── Main Tool Implementation ────────────────────────────────────────────────


async def assess_counterparty_risk(
    wallet_address: str,
    chain_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Assess counterparty risk for an EVM wallet address."""
    validated_input = AssessCounterpartyRiskInput(wallet_address=wallet_address, chain_ids=chain_ids)
    wallet = validated_input.wallet_address
    chains = validated_input.chain_ids

    evm_chains = [cid for c in chains if (cid := _CHAIN_NAME_TO_EVM.get(c)) is not None]
    if not evm_chains:
        evm_chains = [1, 8453]

    tasks: list[Any] = [
        fetch_wallet_positions(wallet, include_net_worth=True, include_token_balances=False),
        fetch_wallet_history(wallet, chain_ids=chains, page_count=20),
        _assess_liquidation(wallet, evm_chains),
    ]
    for chain in chains:
        tasks.append(fetch_wallet_approvals(wallet, chain_id=chain))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    positions_res = results[0]
    history_res = results[1]
    liq_res = results[2]
    approvals_res = results[3:]

    partial_errors: list[str] = []
    risk_factors: list[RiskFactor] = []
    sources_succeeded = 0
    source_urls: list[str] = []
    fetched_at_timestamps: list[str] = []
    cache_hits: list[bool] = []

    has_high_risk = False
    has_caution = False

    # 1. Process positions
    total_net_worth: float | None = None
    if isinstance(positions_res, Exception):
        partial_errors.append(f"Positions fetch failed: {positions_res}")
    elif isinstance(positions_res, DebankWalletSnapshot):
        if positions_res.errors:
            for err in positions_res.errors:
                partial_errors.append(f"Positions {err.operation}: {err.message}")
        else:
            sources_succeeded += 1
            if positions_res.total_balance:
                val = positions_res.total_balance.get("total_usd_value")
                if val is not None:
                    try:
                        total_net_worth = round(float(val), 2)
                    except (TypeError, ValueError):
                        pass
        source_urls.extend(positions_res.source_urls)
        if positions_res.source_fetched_at:
            fetched_at_timestamps.append(positions_res.source_fetched_at)
        cache_hits.append(positions_res.cache_hit)

    # 2. Process history
    activity = ActivitySummary()
    if isinstance(history_res, Exception):
        partial_errors.append(f"History fetch failed: {history_res}")
    elif isinstance(history_res, DebankEndpointSnapshot):
        if history_res.error is not None:
            partial_errors.append(f"History {history_res.error.operation}: {history_res.error.message}")
        elif isinstance(history_res.payload, dict):
            sources_succeeded += 1
            h_list = history_res.payload.get("history_list") or []
            activity.tx_count = len(h_list)
            counterparties: set[str] = set()
            for tx in h_list:
                if isinstance(tx, dict):
                    for s in tx.get("sends") or []:
                        if isinstance(s, dict) and s.get("to_addr"):
                            counterparties.add(str(s["to_addr"]).lower())
                    for r in tx.get("receives") or []:
                        if isinstance(r, dict) and r.get("from_addr"):
                            counterparties.add(str(r["from_addr"]).lower())
                    if tx.get("other_addr"):
                        counterparties.add(str(tx["other_addr"]).lower())
            activity.distinct_counterparties = len(counterparties)
            cex_dict = history_res.payload.get("cex_dict") or {}
            activity.cex_names = sorted(
                list({info.get("name") for info in cex_dict.values() if isinstance(info, dict) and info.get("name")})
            )
        source_urls.append(history_res.source_url)
        if history_res.source_fetched_at:
            fetched_at_timestamps.append(history_res.source_fetched_at)
        cache_hits.append(history_res.cache_hit)

    # 3. Process liquidation
    liquidation = LiquidationSummary()
    if isinstance(liq_res, Exception):
        partial_errors.append(f"Liquidation scan failed: {liq_res}")
    else:
        liq_summary, liq_factors, liq_errs = liq_res
        liquidation = liq_summary
        risk_factors.extend(liq_factors)
        if liq_errs:
            partial_errors.extend(liq_errs)
        else:
            sources_succeeded += 1

        if any(f.severity == "high" for f in liq_factors):
            has_high_risk = True
        elif any(f.severity == "medium" for f in liq_factors):
            has_caution = True

    # 4. Process approvals
    approval_summary = ApprovalSummary()
    approvals_any_success = False
    total_exposure = 0.0

    for idx, app_res in enumerate(approvals_res):
        chain_name = chains[idx] if idx < len(chains) else "unknown"
        if isinstance(app_res, Exception):
            partial_errors.append(f"Approvals ({chain_name}) failed: {app_res}")
        elif isinstance(app_res, DebankEndpointSnapshot):
            if app_res.error is not None:
                partial_errors.append(f"Approvals {app_res.error.operation} ({chain_name}): {app_res.error.message}")
            elif isinstance(app_res.payload, list):
                approvals_any_success = True
                for token in app_res.payload:
                    if not isinstance(token, dict):
                        continue
                    token_sum_exp = float(token.get("sum_exposure_usd") or 0.0)
                    total_exposure += token_sum_exp
                    token_has_unlimited = False
                    for spender in token.get("spenders") or []:
                        if not isinstance(spender, dict):
                            continue
                        approval_summary.total += 1
                        sp_id = spender.get("id") or "unknown"
                        is_hacked = spender.get("is_hacked") is True
                        is_abandoned = spender.get("is_abandoned") is True
                        is_open_source = spender.get("is_open_source")
                        unlimited = _is_unlimited(spender.get("value"))
                        sp_exp = float(spender.get("exposure_usd") or 0.0)

                        if is_hacked:
                            approval_summary.hacked_spenders += 1
                            has_high_risk = True
                            risk_factors.append(
                                RiskFactor(
                                    category="approvals",
                                    severity="high",
                                    detail=f"Spender {sp_id} on {chain_name} is flagged as hacked",
                                )
                            )
                        if unlimited:
                            approval_summary.unlimited += 1
                            token_has_unlimited = True
                            if sp_exp > 10000.0:
                                has_high_risk = True
                                exp_str = f"${sp_exp:,.2f}"
                                risk_factors.append(
                                    RiskFactor(
                                        category="approvals",
                                        severity="high",
                                        detail=f"Unlimited approval with high exposure ({exp_str}) on {sp_id} ({chain_name})",
                                    )
                                )
                            else:
                                has_caution = True
                                risk_factors.append(
                                    RiskFactor(
                                        category="approvals",
                                        severity="medium",
                                        detail=f"Unlimited approval on spender {sp_id} ({chain_name})",
                                    )
                                )
                        if is_abandoned:
                            approval_summary.abandoned_spenders += 1
                            has_caution = True
                            risk_factors.append(
                                RiskFactor(
                                    category="approvals",
                                    severity="medium",
                                    detail=f"Spender {sp_id} on {chain_name} is flagged as abandoned",
                                )
                            )
                        if is_open_source is False:
                            approval_summary.unverified_spenders += 1
                            has_caution = True
                            risk_factors.append(
                                RiskFactor(
                                    category="approvals",
                                    severity="medium",
                                    detail=f"Spender {sp_id} on {chain_name} is closed-source / unverified",
                                )
                            )
                    if token_has_unlimited and token_sum_exp > 10000.0:
                        has_high_risk = True

            source_urls.append(app_res.source_url)
            if app_res.source_fetched_at:
                fetched_at_timestamps.append(app_res.source_fetched_at)
            cache_hits.append(app_res.cache_hit)

    if approvals_any_success:
        sources_succeeded += 1

    approval_summary.sum_exposure_usd = round(total_exposure, 2)

    # 5. Check total failure
    if sources_succeeded == 0:
        err_msg = "; ".join(partial_errors) or "no sources returned data"
        raise CounterpartyRiskError(f"All counterparty risk data sources failed for {wallet}: {err_msg}")

    # 6. Determine verdict
    if has_high_risk:
        verdict = "high_risk"
    elif sources_succeeded < 2:
        verdict = "insufficient_data"
    elif has_caution:
        verdict = "caution"
    else:
        verdict = "acceptable"

    latest_fetched_at = max(fetched_at_timestamps) if fetched_at_timestamps else None
    provenance = DataProvenance(
        provider="DeBank Cloud & On-Chain RPC",
        source_urls=sorted(list(set(source_urls))),
        retrieved_at=utc_now_iso(),
        source_fetched_at=latest_fetched_at,
        cache_hit=all(cache_hits) if cache_hits else False,
        cache_age_seconds=cache_age_seconds(latest_fetched_at) if latest_fetched_at else None,
        cache_ttl_seconds=60,
    )

    output = AssessCounterpartyRiskOutput(
        wallet_address=wallet,
        verdict=verdict,
        risk_factors=risk_factors,
        approval_summary=approval_summary,
        liquidation=liquidation,
        activity=activity,
        total_net_worth_usd=total_net_worth,
        partial_errors=partial_errors,
        data_complete=len(partial_errors) == 0,
        provenance=provenance,
        as_of=utc_now_iso(),
    )
    return output.model_dump()


TOOL = ToolDefinition(
    name="assess_counterparty_risk",
    version="1.0.0",
    description=(
        "Assess counterparty risk for an EVM address across token approvals, liquidation health, "
        "and activity history. Returns an agent-branchable verdict (high_risk, caution, acceptable, "
        "or insufficient_data) and compact risk summaries."
    ),
    use_when="Before sending funds, delegating authority, or interacting with an unknown EVM address.",
    limitations=(
        "Analytics and heuristic risk assessment; not a credit bureau, fraud guarantee, or legal endorsement. "
        "Relies on DeBank indexing and on-chain Aave/Compound contract states."
    ),
    alternatives=["get_wallet_approvals", "get_liquidation_risk", "get_wallet_positions"],
    tags=["risk", "counterparty", "security", "approvals", "liquidation", "web3"],
    input_schema=AssessCounterpartyRiskInput,
    output_schema=AssessCounterpartyRiskOutput,
    implementation=assess_counterparty_risk,
)
