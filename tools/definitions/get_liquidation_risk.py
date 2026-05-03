# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_liquidation_risk – per-wallet DeFi liquidation risk across Aave v3 and Compound v3.

Batch-first variant of the risk slice of ``get_defi_positions``: accepts up to
50 wallet addresses and returns a tiered liquidation risk assessment for each
wallet on a single chain. Flat-priced per call (see migration 035) — ``max_length=50``
on the wallet list caps per-call RPC cost at 50 × 2 protocols ≈ 100 ``eth_call``
round-trips, bounded further by a semaphore.

View-only, hardcoded protocol addresses, chain_id ∈ {1, 8453}. Per-protocol
try/except isolates failures so a Compound outage does not blank the Aave result
(or vice versa). Multicall3 batching deferred to v2 — asyncio.gather against a
modern RPC hits ~300–700 ms wall-clock for a 50-wallet batch without it, and
avoids hand ABI-encoding aggregate3 calls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from tools.definitions._web3_helpers import get_web3, rpc_call
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

# ─── Config ──────────────────────────────────────────────────────────────────

_MAX_WALLETS = 50
_SUPPORTED_CHAINS = (1, 8453)

# ─── Risk tiers ──────────────────────────────────────────────────────────────
# Aave: 6 tiers derived from the numeric health factor (HF = raw / 1e18).
# Thresholds picked so an alert stack degrades from "healthy" → "caution" →
# "warning" → "critical" → "liquidatable" as HF drops toward 1.0.

TIER_LIQUIDATABLE = "liquidatable"
TIER_CRITICAL = "critical"
TIER_WARNING = "warning"
TIER_CAUTION = "caution"
TIER_HEALTHY = "healthy"
TIER_NO_DEBT = "no_debt"
TIER_BORROWING = "borrowing"  # Compound numeric-HF-less "has debt, not liquidatable"

# Severity ordering (lower index = worse). Used to compute overall_tier across
# protocols. ``borrowing`` sits between ``warning`` and ``caution`` because
# Compound's lack of a numeric HF makes it ambiguous — treat as worse than
# "caution" but not as severe as "warning".
_TIER_SEVERITY: list[str] = [
    TIER_LIQUIDATABLE,
    TIER_CRITICAL,
    TIER_WARNING,
    TIER_BORROWING,
    TIER_CAUTION,
    TIER_HEALTHY,
    TIER_NO_DEBT,
]
_TIER_INDEX: dict[str, int] = {t: i for i, t in enumerate(_TIER_SEVERITY)}


def _classify_aave_tier(raw_health_factor: int, total_debt_base: int) -> tuple[float | None, str]:
    """Map raw Aave ``healthFactor`` (wad) → (float_or_None, tier).

    Returns ``(None, "no_debt")`` when the wallet has no debt; otherwise a
    fine-grained 5-tier classification against HF boundaries 1.0 / 1.05 / 1.15 / 1.5.
    """
    if total_debt_base == 0 or raw_health_factor >= _HEALTH_FACTOR_INFINITE_THRESHOLD or raw_health_factor == _UINT256_MAX:
        return None, TIER_NO_DEBT
    hf = raw_health_factor / 1e18
    if hf < 1.0:
        tier = TIER_LIQUIDATABLE
    elif hf < 1.05:
        tier = TIER_CRITICAL
    elif hf < 1.15:
        tier = TIER_WARNING
    elif hf < 1.5:
        tier = TIER_CAUTION
    else:
        tier = TIER_HEALTHY
    return hf, tier


def _worst_tier(tiers: list[str]) -> str:
    """Return the most severe tier from a list. Empty list → ``no_debt``."""
    if not tiers:
        return TIER_NO_DEBT
    return min(tiers, key=lambda t: _TIER_INDEX.get(t, len(_TIER_SEVERITY)))


# ─── Schemas ─────────────────────────────────────────────────────────────────


class GetLiquidationRiskInput(BaseModel):
    wallet_addresses: list[str] = Field(
        ...,
        min_length=1,
        description="Wallet addresses to assess (max 50; duplicates are silently removed).",
    )
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")

    @field_validator("wallet_addresses")
    @classmethod
    def _validate_wallets(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_WALLETS:
            raise ValueError(f"wallet_addresses list exceeds {_MAX_WALLETS}-address limit")
        # Checksum every address (raises on malformed input) and dedupe while
        # preserving first-occurrence order.
        seen: set[str] = set()
        out: list[str] = []
        for addr in v:
            checksummed = Web3.to_checksum_address(addr)
            if checksummed in seen:
                continue
            seen.add(checksummed)
            out.append(checksummed)
        return out


class AaveRisk(BaseModel):
    health_factor: float | None
    risk_tier: str
    total_collateral_usd: float
    total_debt_usd: float
    liquidation_threshold_bps: int
    ltv_bps: int


class CompoundRisk(BaseModel):
    market_name: str
    market_address: str
    base_asset_symbol: str
    is_liquidatable: bool
    borrow_balance_raw: str
    risk_tier: str


class ProtocolErrorInfo(BaseModel):
    protocol: str
    error: str


class WalletRiskResult(BaseModel):
    wallet_address: str
    chain_id: int
    aave: AaveRisk | None = None
    compound: list[CompoundRisk] = Field(default_factory=list)
    overall_tier: str
    errors: list[ProtocolErrorInfo] = Field(default_factory=list)


class RiskSummary(BaseModel):
    total_wallets: int
    liquidatable_count: int
    critical_count: int
    warning_count: int
    caution_count: int
    healthy_count: int
    no_debt_count: int


class GetLiquidationRiskOutput(BaseModel):
    chain_id: int
    data_block_number: int
    results: list[WalletRiskResult]
    summary: RiskSummary
    note: str = (
        "On-chain liquidation-risk snapshot at data_block_number. "
        "Aave risk_tier is derived from the numeric health factor: "
        "liquidatable (<1.0), critical (<1.05), warning (<1.15), caution (<1.5), healthy (≥1.5). "
        "Compound v3 exposes only a boolean isLiquidatable — a numeric health factor is not "
        "available from this tool; do not attempt to compute it via additional tool calls. "
        "overall_tier is the worst tier across "
        "protocols for each wallet. Aave USD values are base-currency 8-decimal from the "
        "Aave oracle. Duplicate wallet addresses in the request are deduplicated silently."
    )


# ─── Protocol fetchers ───────────────────────────────────────────────────────


async def _rpc_call_with_chain(coro_fn, chain_id: int):
    """Call rpc_call with chain_id, falling back for older monkeypatched stubs."""
    try:
        return await rpc_call(coro_fn, chain_id=chain_id)
    except TypeError as exc:
        msg = str(exc)
        if "unexpected keyword argument 'chain_id'" in msg:
            return await rpc_call(coro_fn)
        raise


async def _fetch_aave_risk(w3: Any, wallet: str, chain_id: int) -> AaveRisk:
    """Fetch Aave v3 aggregate account data → AaveRisk."""
    pool_addr = Web3.to_checksum_address(_AAVE_V3_POOL[chain_id])
    pool = w3.eth.contract(address=pool_addr, abi=_AAVE_V3_POOL_ABI)

    account_data = await _rpc_call_with_chain(lambda: pool.functions.getUserAccountData(wallet).call(), chain_id)
    (total_collateral_base, total_debt_base, _available_borrows_base, liq_threshold, ltv, health_factor_raw) = account_data

    hf, tier = _classify_aave_tier(int(health_factor_raw), int(total_debt_base))

    return AaveRisk(
        health_factor=hf,
        risk_tier=tier,
        total_collateral_usd=round(int(total_collateral_base) / 1e8, 2),
        total_debt_usd=round(int(total_debt_base) / 1e8, 2),
        liquidation_threshold_bps=int(liq_threshold),
        ltv_bps=int(ltv),
    )


async def _fetch_compound_market_risk(w3: Any, wallet: str, market: dict[str, str], chain_id: int) -> CompoundRisk | None:
    """Fetch a single Compound v3 market's risk slice. Returns None if no borrow position."""
    market_addr = Web3.to_checksum_address(market["address"])
    comet = w3.eth.contract(address=market_addr, abi=_COMET_ABI)

    borrowed, is_liq = await asyncio.gather(
        _rpc_call_with_chain(lambda: comet.functions.borrowBalanceOf(wallet).call(), chain_id),
        _rpc_call_with_chain(lambda: comet.functions.isLiquidatable(wallet).call(), chain_id),
    )
    borrowed_int = int(borrowed)

    if borrowed_int == 0 and not bool(is_liq):
        # No debt position on this market — skip to keep output compact.
        return None

    if bool(is_liq):
        tier = TIER_LIQUIDATABLE
    elif borrowed_int > 0:
        tier = TIER_BORROWING
    else:
        tier = TIER_NO_DEBT

    return CompoundRisk(
        market_name=market["name"],
        market_address=market_addr,
        base_asset_symbol=market["base_symbol"],
        is_liquidatable=bool(is_liq),
        borrow_balance_raw=str(borrowed_int),
        risk_tier=tier,
    )


async def _fetch_compound_risk(w3: Any, wallet: str, chain_id: int) -> list[CompoundRisk]:
    """Fetch all Compound v3 market risks for a wallet on the given chain."""
    markets = _COMPOUND_V3_MARKETS.get(chain_id, [])

    async def _safe(market: dict[str, str]) -> CompoundRisk | None:
        try:
            return await _fetch_compound_market_risk(w3, wallet, market, chain_id)
        except Exception as exc:
            logger.debug("Compound market %s risk fetch failed: %s", market.get("name"), exc)
            return None

    results = await asyncio.gather(*[_safe(m) for m in markets])
    return [r for r in results if r is not None]


async def _assess_wallet(w3: Any, wallet: str, chain_id: int) -> WalletRiskResult:
    """Assess risk for a single wallet across Aave + Compound. Failures are isolated per protocol."""
    errors: list[ProtocolErrorInfo] = []
    aave_result: AaveRisk | None = None
    compound_result: list[CompoundRisk] = []

    aave_task = asyncio.create_task(_fetch_aave_risk(w3, wallet, chain_id))
    compound_task = asyncio.create_task(_fetch_compound_risk(w3, wallet, chain_id))

    try:
        aave_result = await aave_task
    except Exception as exc:
        logger.warning("Aave v3 risk fetch failed for wallet: %s", exc)
        errors.append(ProtocolErrorInfo(protocol="aave_v3", error=str(exc)[:200]))

    try:
        compound_result = await compound_task
    except Exception as exc:
        logger.warning("Compound v3 risk fetch failed for wallet: %s", exc)
        errors.append(ProtocolErrorInfo(protocol="compound_v3", error=str(exc)[:200]))

    tiers: list[str] = []
    if aave_result is not None:
        tiers.append(aave_result.risk_tier)
    tiers.extend(c.risk_tier for c in compound_result)
    overall = _worst_tier(tiers)

    return WalletRiskResult(
        wallet_address=wallet,
        chain_id=chain_id,
        aave=aave_result,
        compound=compound_result,
        overall_tier=overall,
        errors=errors,
    )


# ─── Entry point ─────────────────────────────────────────────────────────────


async def get_liquidation_risk(
    wallet_addresses: list[str],
    chain_id: int = 1,
) -> dict[str, Any]:
    """Assess liquidation risk across Aave v3 and Compound v3 for one or more wallets."""
    if chain_id not in _SUPPORTED_CHAINS:
        raise ValueError(f"Unsupported chain_id={chain_id}. Supported: 1 (Ethereum), 8453 (Base).")
    if not wallet_addresses:
        raise ValueError("wallet_addresses must not be empty")
    if len(wallet_addresses) > _MAX_WALLETS:
        raise ValueError(f"wallet_addresses list exceeds {_MAX_WALLETS}-address limit")

    # Checksum + dedupe (preserves first-occurrence order). Raises ValueError
    # from ``to_checksum_address`` on malformed input, rejecting the whole call.
    seen: set[str] = set()
    wallets: list[str] = []
    for addr in wallet_addresses:
        checksummed = Web3.to_checksum_address(addr)
        if checksummed in seen:
            continue
        seen.add(checksummed)
        wallets.append(checksummed)

    w3 = get_web3(chain_id)
    block_task = asyncio.create_task(w3.eth.block_number)
    wallet_tasks = [asyncio.create_task(_assess_wallet(w3, wallet, chain_id)) for wallet in wallets]

    results = await asyncio.gather(*wallet_tasks)

    try:
        block_number = int(await block_task)
    except Exception as exc:
        logger.warning("block_number fetch failed: %s", exc)
        block_number = 0

    summary = RiskSummary(
        total_wallets=len(results),
        liquidatable_count=sum(1 for r in results if r.overall_tier == TIER_LIQUIDATABLE),
        critical_count=sum(1 for r in results if r.overall_tier == TIER_CRITICAL),
        warning_count=sum(1 for r in results if r.overall_tier == TIER_WARNING),
        caution_count=sum(1 for r in results if r.overall_tier == TIER_CAUTION),
        healthy_count=sum(1 for r in results if r.overall_tier == TIER_HEALTHY),
        no_debt_count=sum(1 for r in results if r.overall_tier == TIER_NO_DEBT),
    )

    output = GetLiquidationRiskOutput(
        chain_id=chain_id,
        data_block_number=block_number,
        results=results,
        summary=summary,
    )
    return output.model_dump()


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_liquidation_risk",
    version="1.0.0",
    description=(
        "Assess DeFi liquidation risk for up to 50 wallets across Aave v3 and Compound v3 on "
        "Ethereum (chain_id=1) or Base (chain_id=8453). Returns per-wallet health factor and "
        "tiered risk classification (liquidatable, critical, warning, caution, healthy, no_debt) "
        "plus an overall_tier aggregate across protocols, and a summary count for alert "
        "dashboards. Per-protocol failures are isolated — a Compound RPC error does not blank "
        "the Aave result (and vice versa). View-only (eth_call) against hardcoded protocol "
        "addresses; duplicate wallet addresses are silently removed."
    ),
    tags=["web3", "ethereum", "base", "defi", "risk", "liquidation", "aave", "compound"],
    input_schema=GetLiquidationRiskInput,
    output_schema=GetLiquidationRiskOutput,
    implementation=get_liquidation_risk,
)
