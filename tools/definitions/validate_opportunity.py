# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""validate_opportunity – Composite DeFi yield pool sustainability verdict."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel, Field, field_validator

from tools._internals._http_session import get_coingecko_session, get_defillama_session
from tools._internals.provenance import DataProvenance, cache_age_seconds, utc_now_iso
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"
_DEFILLAMA_CHART_URL = "https://yields.llama.fi/chart/{pool_id}"
_COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CACHE_TTL = 300
_SEVERITY_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_STABLECOIN_SYMBOLS: frozenset[str] = frozenset({"usdc", "usdt", "dai", "frax", "lusd", "usde", "pyusd", "gho", "crvusd"})
_STABLECOIN_COINGECKO_IDS: dict[str, str] = {
    "usdc": "usd-coin",
    "usdt": "tether",
    "dai": "dai",
    "frax": "frax",
    "lusd": "liquity-usd",
    "usde": "ethena-usde",
    "pyusd": "paypal-usd",
    "gho": "gho",
    "crvusd": "crvusd",
}

_opportunity_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class OpportunityValidationError(RuntimeError):
    """Raised when all opportunity validation data sources fail."""


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ─── Schemas ──────────────────────────────────────────────────────────────────


class ValidateOpportunityInput(BaseModel):
    pool_id: str = Field(
        ...,
        description="DeFiLlama yield pool UUID (e.g. '747c1d2a-c668-4682-b9f9-296708a3dd90').",
    )

    @field_validator("pool_id")
    @classmethod
    def _validate_pool_id(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not _UUID_RE.fullmatch(cleaned):
            raise ValueError("pool_id must be a valid UUID string (e.g. '747c1d2a-c668-4682-b9f9-296708a3dd90')")
        return cleaned


class RiskFactor(BaseModel):
    category: str
    severity: Literal["high", "medium", "low"]
    detail: str


class YieldSummary(BaseModel):
    apy: float = 0.0
    apy_base: float | None = None
    apy_reward: float | None = None
    apy_mean_30d: float | None = None
    reward_share_pct: float | None = None
    is_spiking: bool = False


class LiquiditySummary(BaseModel):
    tvl_usd: float = 0.0
    tvl_30d_change_pct: float | None = None
    chart_days: int = 0
    drain_detected: bool = False


class ValidateOpportunityOutput(BaseModel):
    pool_id: str
    project: str = ""
    symbol: str = ""
    chain: str = ""
    verdict: Literal["sustainable", "caution", "unsustainable", "insufficient_data"]
    sustainability_reason: str
    risk_factors: list[RiskFactor] = Field(default_factory=list)
    yield_summary: YieldSummary = Field(default_factory=YieldSummary)
    liquidity_summary: LiquiditySummary = Field(default_factory=LiquiditySummary)
    partial_errors: list[str] = Field(default_factory=list)
    data_complete: bool = True
    provenance: DataProvenance
    as_of: str


# ─── Data Fetchers ────────────────────────────────────────────────────────────


async def _fetch_pool_snapshot(pool_id: str) -> dict[str, Any]:
    session = await get_defillama_session()
    async with session.get(_DEFILLAMA_POOLS_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"DeFiLlama /pools returned status {resp.status}")
        payload: dict[str, Any] = await resp.json()
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise ValueError("Invalid DeFiLlama /pools response format")
        for pool in data:
            if isinstance(pool, dict) and pool.get("pool") == pool_id:
                return pool
    raise ValueError(f"Pool {pool_id} not found in DeFiLlama yields data")


async def _fetch_pool_chart(pool_id: str) -> list[dict[str, Any]]:
    session = await get_defillama_session()
    url = _DEFILLAMA_CHART_URL.format(pool_id=pool_id)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"DeFiLlama /chart returned status {resp.status}")
        payload: dict[str, Any] = await resp.json()
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise ValueError("Invalid DeFiLlama /chart response format")
        return [pt for pt in data if isinstance(pt, dict)]


async def _fetch_coingecko_stable_price(coin_id: str) -> float | None:
    session = await get_coingecko_session()
    params = {"ids": coin_id, "vs_currencies": "usd"}
    async with session.get(_COINGECKO_PRICE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"CoinGecko simple/price returned status {resp.status}")
        payload = await resp.json()
        if isinstance(payload, dict) and coin_id in payload:
            return _safe_float(payload[coin_id].get("usd"), default=-1.0)
    return None


# ─── Risk & Verdict Evaluation ────────────────────────────────────────────────


def _evaluate_opportunity(
    pool_id: str,
    pool_data: dict[str, Any] | None,
    chart_data: list[dict[str, Any]] | None,
    stable_price: float | None,
    sources_succeeded: int,
    partial_errors: list[str],
    source_urls: list[str],
) -> ValidateOpportunityOutput:
    risk_factors: list[RiskFactor] = []
    project = ""
    symbol = ""
    chain = ""

    # Yield evaluation
    apy = 0.0
    apy_base: float | None = None
    apy_reward: float | None = None
    apy_mean_30d: float | None = None
    reward_share_pct: float | None = None
    is_spiking = False

    if pool_data:
        project = str(pool_data.get("project") or "")
        symbol = str(pool_data.get("symbol") or "")
        chain = str(pool_data.get("chain") or "")
        apy = _safe_float(pool_data.get("apy"), default=0.0)
        apy_base = _safe_float(pool_data.get("apyBase")) if pool_data.get("apyBase") is not None else None
        apy_reward = _safe_float(pool_data.get("apyReward")) if pool_data.get("apyReward") is not None else None
        apy_mean_30d = _safe_float(pool_data.get("apyMean30d")) if pool_data.get("apyMean30d") is not None else None

        if apy > 0 and apy_reward is not None and apy_reward > 0:
            reward_share_pct = round(min(100.0, (apy_reward / apy) * 100.0), 1)

        if apy_mean_30d is not None and apy_mean_30d > 0 and apy >= 3.0 * apy_mean_30d:
            is_spiking = True

        if apy > 100.0:
            risk_factors.append(
                RiskFactor(
                    category="yield",
                    severity="high",
                    detail=f"Extremely high APY ({apy:.1f}%) indicates unsustainable emissions or ponzinomics.",
                )
            )
        elif apy < 0.0:
            risk_factors.append(
                RiskFactor(
                    category="yield",
                    severity="high",
                    detail=f"Negative APY ({apy:.1f}%) detected on pool.",
                )
            )

        if reward_share_pct is not None and reward_share_pct > 80.0 and apy > 10.0:
            risk_factors.append(
                RiskFactor(
                    category="yield",
                    severity="medium",
                    detail=(
                        f"Yield is heavily emission-dependent ({reward_share_pct:.1f}% from rewards) rather than organic fees."
                    ),
                )
            )

        if is_spiking:
            risk_factors.append(
                RiskFactor(
                    category="yield",
                    severity="medium",
                    detail=f"Current APY ({apy:.1f}%) is 3x or higher than 30-day mean ({apy_mean_30d:.1f}%).",
                )
            )

        il_risk = str(pool_data.get("ilRisk") or "").lower()
        if il_risk == "yes":
            risk_factors.append(
                RiskFactor(
                    category="impermanent_loss",
                    severity="low",
                    detail="Pool is subject to impermanent loss risk.",
                )
            )

    yield_summary = YieldSummary(
        apy=apy,
        apy_base=apy_base,
        apy_reward=apy_reward,
        apy_mean_30d=apy_mean_30d,
        reward_share_pct=reward_share_pct,
        is_spiking=is_spiking,
    )

    # Liquidity & chart evaluation
    tvl_usd = _safe_float(pool_data.get("tvlUsd")) if pool_data else 0.0
    tvl_30d_change_pct: float | None = None
    chart_days = len(chart_data) if chart_data else 0
    drain_detected = False

    if chart_data and len(chart_data) >= 2:
        start_idx = max(0, len(chart_data) - 30)
        start_tvl = _safe_float(chart_data[start_idx].get("tvlUsd"))
        end_tvl = _safe_float(chart_data[-1].get("tvlUsd"))
        if start_tvl > 0:
            tvl_30d_change_pct = round(((end_tvl - start_tvl) / start_tvl) * 100.0, 2)
            if tvl_30d_change_pct < -50.0:
                drain_detected = True
                risk_factors.append(
                    RiskFactor(
                        category="liquidity",
                        severity="high",
                        detail=f"Severe TVL contraction ({tvl_30d_change_pct:.1f}% over 30 days) signals liquidity flight.",
                    )
                )
            elif tvl_30d_change_pct < -20.0:
                risk_factors.append(
                    RiskFactor(
                        category="liquidity",
                        severity="medium",
                        detail=f"Notable TVL contraction ({tvl_30d_change_pct:.1f}% over 30 days) detected.",
                    )
                )

    if pool_data:
        if tvl_usd < 100_000.0:
            risk_factors.append(
                RiskFactor(
                    category="liquidity",
                    severity="high",
                    detail=f"Critically low TVL (${tvl_usd:,.0f} < $100k) presents severe slippage and exit risk.",
                )
            )
        elif tvl_usd < 1_000_000.0:
            risk_factors.append(
                RiskFactor(
                    category="liquidity",
                    severity="medium",
                    detail=f"Low TVL (${tvl_usd:,.0f} < $1M) may restrict deployment capacity.",
                )
            )

    if chart_days > 0 and chart_days < 7:
        risk_factors.append(
            RiskFactor(
                category="track_record",
                severity="medium",
                detail=f"Pool has only {chart_days} days of recorded history.",
            )
        )

    # Peg check for stablecoin pools
    if stable_price is not None and stable_price > 0:
        if abs(stable_price - 1.0) > 0.05:
            risk_factors.append(
                RiskFactor(
                    category="peg_stability",
                    severity="high",
                    detail=f"Underlying stablecoin price (${stable_price:.3f}) is de-pegged by more than 5%.",
                )
            )

    liquidity_summary = LiquiditySummary(
        tvl_usd=tvl_usd,
        tvl_30d_change_pct=tvl_30d_change_pct,
        chart_days=chart_days,
        drain_detected=drain_detected,
    )

    # Sort risk factors deterministically
    risk_factors.sort(key=lambda rf: (_SEVERITY_ORDER.get(rf.severity, 99), rf.category, rf.detail))

    # Verdict fold
    if pool_data is None or chart_data is None or sources_succeeded < 2:
        verdict = "insufficient_data"
        sustainability_reason = "Core pool metrics or historical chart unavailable; insufficient data to evaluate sustainability."
    else:
        has_high = any(rf.severity == "high" for rf in risk_factors)
        has_medium = any(rf.severity == "medium" for rf in risk_factors)
        if has_high:
            if tvl_usd < 100_000.0 or drain_detected or apy > 200.0:
                verdict = "unsustainable"
            else:
                verdict = "caution"
            sustainability_reason = risk_factors[0].detail
        elif has_medium:
            verdict = "caution"
            sustainability_reason = risk_factors[0].detail
        else:
            verdict = "sustainable"
            sustainability_reason = "Pool exhibits healthy liquidity, consistent yields, and stable track record."

    provenance = DataProvenance(
        provider="DeFiLlama & CoinGecko",
        source_urls=sorted(list(set(source_urls))),
        retrieved_at=utc_now_iso(),
        cache_hit=False,
        cache_ttl_seconds=_CACHE_TTL,
    )

    return ValidateOpportunityOutput(
        pool_id=pool_id,
        project=project,
        symbol=symbol,
        chain=chain,
        verdict=verdict,
        sustainability_reason=sustainability_reason,
        risk_factors=risk_factors,
        yield_summary=yield_summary,
        liquidity_summary=liquidity_summary,
        partial_errors=partial_errors,
        data_complete=len(partial_errors) == 0,
        provenance=provenance,
        as_of=utc_now_iso(),
    )


# ─── Tool Implementation ──────────────────────────────────────────────────────


async def validate_opportunity(pool_id: str) -> dict[str, Any]:
    """Validate a DeFi yield pool opportunity across yields, liquidity, and stability."""
    input_data = ValidateOpportunityInput(pool_id=pool_id)
    cache_k = input_data.pool_id

    now = time.monotonic()
    cached = _opportunity_cache.get(cache_k)
    if cached and now < cached[0]:
        cached_result = dict(cached[1])
        if "provenance" in cached_result and isinstance(cached_result["provenance"], dict):
            prov = dict(cached_result["provenance"])
            prov["cache_hit"] = True
            prov["cache_age_seconds"] = cache_age_seconds(prov.get("retrieved_at"))
            cached_result["provenance"] = prov
        return cached_result

    pool_task = asyncio.create_task(_fetch_pool_snapshot(input_data.pool_id))
    chart_task = asyncio.create_task(_fetch_pool_chart(input_data.pool_id))

    pool_res, chart_res = await asyncio.gather(pool_task, chart_task, return_exceptions=True)

    partial_errors: list[str] = []
    source_urls: list[str] = []
    sources_succeeded = 0

    pool_data: dict[str, Any] | None = None
    chart_data: list[dict[str, Any]] | None = None

    if isinstance(pool_res, Exception):
        partial_errors.append(f"DeFiLlama pool snapshot failed: {pool_res}")
    else:
        pool_data = pool_res
        sources_succeeded += 1
        source_urls.append(_DEFILLAMA_POOLS_URL)

    if isinstance(chart_res, Exception):
        partial_errors.append(f"DeFiLlama chart history failed: {chart_res}")
    else:
        chart_data = chart_res
        sources_succeeded += 1
        source_urls.append(_DEFILLAMA_CHART_URL.format(pool_id=input_data.pool_id))

    # Optional spot stablecoin validation if symbol or stablecoin flag matches
    stable_price: float | None = None
    if pool_data:
        sym = str(pool_data.get("symbol") or "").lower()
        is_stable = bool(pool_data.get("stablecoin")) or any(s in sym for s in _STABLECOIN_SYMBOLS)
        if is_stable:
            matched_id: str | None = None
            for s, cg_id in _STABLECOIN_COINGECKO_IDS.items():
                if s in sym:
                    matched_id = cg_id
                    break
            if matched_id:
                try:
                    stable_price = await _fetch_coingecko_stable_price(matched_id)
                    if stable_price is not None and stable_price > 0:
                        sources_succeeded += 1
                        source_urls.append(_COINGECKO_PRICE_URL)
                except Exception as exc:
                    partial_errors.append(f"CoinGecko price check failed: {exc}")

    if sources_succeeded == 0:
        err_msg = "; ".join(partial_errors) or "no sources returned data"
        raise OpportunityValidationError(
            f"All opportunity validation data sources failed for pool {input_data.pool_id}: {err_msg}"
        )

    output = _evaluate_opportunity(
        pool_id=input_data.pool_id,
        pool_data=pool_data,
        chart_data=chart_data,
        stable_price=stable_price,
        sources_succeeded=sources_succeeded,
        partial_errors=partial_errors,
        source_urls=source_urls,
    )

    result_dict = output.model_dump()
    _opportunity_cache[cache_k] = (now + _CACHE_TTL, result_dict)
    return result_dict


TOOL = ToolDefinition(
    name="validate_opportunity",
    version="1.0.0",
    description=(
        "Assess the economic sustainability and liquidity risk of a DeFi yield pool using DeFiLlama "
        "metrics, historical TVL drawdown charts, and token price stability. Returns an agent-branchable "
        "verdict (sustainable, caution, unsustainable, or insufficient_data) and structured risk factors."
    ),
    use_when=(
        "Before deploying capital into a DeFi yield pool or staking opportunity to verify "
        "economic sustainability and liquidity health."
    ),
    limitations=(
        "Heuristic economic assessment based on DeFiLlama analytics and CoinGecko pricing. "
        "Does not audit smart contract bytecode, protocol governance, or admin key security."
    ),
    alternatives=["get_yield_rates", "get_protocol_tvl", "get_lending_rates", "assess_counterparty_risk"],
    tags=["defi", "yield", "risk", "validation", "opportunity", "liquidity"],
    input_schema=ValidateOpportunityInput,
    output_schema=ValidateOpportunityOutput,
    implementation=validate_opportunity,
)
