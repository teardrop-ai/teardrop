# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_lending_rates - on-chain lending supply/borrow rates for Aave v3 and Compound v3.

Read-only market-rate snapshot (no wallet required). Supports Ethereum (1) and Base
(8453), reusing Teardrop's hardened RPC helpers and Multicall3 batching.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from tools.definitions._multicall3 import multicall3_batch
from tools.definitions._web3_helpers import get_web3, rpc_call
from tools.definitions.get_defi_positions import (
    _AAVE_V3_DATA_PROVIDER,
    _AAVE_V3_TRACKED_RESERVES,
    _COMPOUND_V3_MARKETS,
)
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# 5-minute cache is sufficient for planner flows while preventing repeated RPC fan-outs.
_RATE_CACHE_TTL = 300
_rates_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_SUPPORTED_PROTOCOLS = {"aave-v3", "compound-v3", "all"}
_SUPPORTED_CHAINS = tuple(sorted({*set(_AAVE_V3_DATA_PROVIDER.keys()), *set(_COMPOUND_V3_MARKETS.keys())}))
_MAX_ASSET_FILTER = 20

# Selectors for batched read-only calls.
_GET_RESERVE_DATA_SELECTOR: bytes = bytes(Web3.keccak(text="getReserveData(address)"))[:4]
_GET_UTILIZATION_SELECTOR: bytes = bytes(Web3.keccak(text="getUtilization()"))[:4]
_GET_SUPPLY_RATE_SELECTOR: bytes = bytes(Web3.keccak(text="getSupplyRate(uint)"))[:4]
_GET_BORROW_RATE_SELECTOR: bytes = bytes(Web3.keccak(text="getBorrowRate(uint)"))[:4]


class GetLendingRatesInput(BaseModel):
    protocol: Literal["all", "aave-v3", "compound-v3"] = Field(
        default="all",
        description="Protocol to query: 'aave-v3', 'compound-v3', or 'all'.",
    )
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base).")
    assets: list[str] | None = Field(
        default=None,
        description=(
            "Optional asset-symbol filter (e.g., ['USDC','DAI']). "
            "Case-insensitive. Max 20 symbols."
        ),
    )

    @field_validator("chain_id")
    @classmethod
    def _validate_chain(cls, v: int) -> int:
        if v not in _SUPPORTED_CHAINS:
            raise ValueError(f"Unsupported chain_id={v}. Supported: {', '.join(str(c) for c in _SUPPORTED_CHAINS)}")
        return v

    @field_validator("assets")
    @classmethod
    def _validate_assets(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) > _MAX_ASSET_FILTER:
            raise ValueError(f"assets list exceeds {_MAX_ASSET_FILTER}-symbol limit")
        out: list[str] = []
        for symbol in v:
            cleaned = str(symbol).strip().upper()
            if cleaned:
                out.append(cleaned)
        return out


class LendingRateEntry(BaseModel):
    protocol: Literal["aave-v3", "compound-v3"]
    chain_id: int
    market_name: str
    asset_symbol: str
    supply_apy_pct: float
    borrow_apy_pct: float
    utilization_pct: float | None = None
    source: Literal["on-chain"] = "on-chain"


class GetLendingRatesOutput(BaseModel):
    protocol: str
    chain_id: int
    data_block_number: int
    rates: list[LendingRateEntry]
    errors: list[str] = Field(default_factory=list)
    note: str = (
        "On-chain lending market snapshot at data_block_number. "
        "Aave rates are derived from ProtocolDataProvider.getReserveData (ray-scaled). "
        "Compound rates are derived from Comet getUtilization/getSupplyRate/getBorrowRate "
        "(per-second 1e18-scaled), annualized to APY."
    )


def _ray_to_apy_pct(rate_ray: int) -> float:
    """Convert Aave ray-scaled annual rate into APY percentage."""
    if rate_ray <= 0:
        return 0.0
    annual_rate = rate_ray / 1e27
    return round(annual_rate * 100.0, 4)


def _per_second_rate_to_apy_pct(rate_per_second: int) -> float:
    """Convert Compound per-second 1e18-scaled rate into APY percentage."""
    if rate_per_second <= 0:
        return 0.0
    per_second = rate_per_second / 1e18
    apy = ((1.0 + per_second) ** 31_536_000 - 1.0) * 100.0
    return round(apy, 4)


def _utilization_to_pct(utilization_scaled: int) -> float:
    if utilization_scaled <= 0:
        return 0.0
    return round((utilization_scaled / 1e18) * 100.0, 4)


def _cache_key(protocol: str, chain_id: int, assets: list[str] | None) -> str:
    if not assets:
        return f"{protocol}:{chain_id}:all-assets"
    unique_sorted = sorted({a.strip().upper() for a in assets if a and a.strip()})
    return f"{protocol}:{chain_id}:{','.join(unique_sorted)}"


async def _fetch_aave_rates(chain_id: int, assets_filter: set[str] | None) -> list[LendingRateEntry]:
    reserves = _AAVE_V3_TRACKED_RESERVES.get(chain_id, [])
    if not reserves:
        return []

    data_provider = Web3.to_checksum_address(_AAVE_V3_DATA_PROVIDER[chain_id])
    w3 = get_web3(chain_id)

    calls = [
        (
            data_provider,
            _GET_RESERVE_DATA_SELECTOR + abi_encode(["address"], [Web3.to_checksum_address(r["address"])]),
        )
        for r in reserves
    ]
    results = await multicall3_batch(w3, calls, chain_id=chain_id)

    entries: list[LendingRateEntry] = []
    for reserve, (success, return_data) in zip(reserves, results):
        symbol = str(reserve.get("symbol", "")).upper()
        if assets_filter is not None and symbol not in assets_filter:
            continue
        if not success or not return_data:
            logger.debug("get_lending_rates: aave reserve call reverted for %s", symbol or "unknown")
            continue
        try:
            # ProtocolDataProvider.getReserveData(asset):
            # (availableLiquidity,totalStableDebt,totalVariableDebt,liquidityRate,
            #  variableBorrowRate,stableBorrowRate,averageStableBorrowRate,
            #  liquidityIndex,variableBorrowIndex,lastUpdateTimestamp)
            decoded = abi_decode(
                [
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint40",
                ],
                return_data,
            )
            liquidity_rate = int(decoded[3])
            variable_borrow_rate = int(decoded[4])
            entries.append(
                LendingRateEntry(
                    protocol="aave-v3",
                    chain_id=chain_id,
                    market_name="Aave v3",
                    asset_symbol=symbol,
                    supply_apy_pct=_ray_to_apy_pct(liquidity_rate),
                    borrow_apy_pct=_ray_to_apy_pct(variable_borrow_rate),
                    utilization_pct=None,
                )
            )
        except Exception as exc:
            logger.debug("get_lending_rates: failed to decode aave reserve data for %s: %s", symbol, exc)

    return entries


async def _fetch_compound_rates(chain_id: int, assets_filter: set[str] | None) -> list[LendingRateEntry]:
    markets = _COMPOUND_V3_MARKETS.get(chain_id, [])
    if not markets:
        return []

    w3 = get_web3(chain_id)

    market_addrs: list[str] = []
    market_symbols: list[str] = []
    market_names: list[str] = []
    utilization_calls: list[tuple[str, bytes]] = []
    for market in markets:
        symbol = str(market.get("base_symbol", "")).upper()
        if assets_filter is not None and symbol not in assets_filter:
            continue
        market_addr = Web3.to_checksum_address(str(market["address"]))
        utilization_calls.append((market_addr, _GET_UTILIZATION_SELECTOR))
        market_addrs.append(market_addr)
        market_symbols.append(symbol)
        market_names.append(str(market.get("name", "Compound v3")))

    if not utilization_calls:
        return []

    utilization_results = await multicall3_batch(w3, utilization_calls, chain_id=chain_id)
    utilization_values: list[int | None] = []
    for symbol, (success, return_data) in zip(market_symbols, utilization_results):
        if not success or not return_data:
            logger.debug("get_lending_rates: compound utilization call failed for %s", symbol)
            utilization_values.append(None)
            continue
        try:
            utilization_values.append(int(abi_decode(["uint256"], return_data)[0]))
        except Exception as exc:
            logger.debug("get_lending_rates: failed to decode compound utilization for %s: %s", symbol, exc)
            utilization_values.append(None)

    rate_calls: list[tuple[str, bytes]] = []
    rate_indices: list[int] = []
    for idx, utilization in enumerate(utilization_values):
        if utilization is None:
            continue
        rate_indices.append(idx)
        rate_calls.append((market_addrs[idx], _GET_SUPPLY_RATE_SELECTOR + abi_encode(["uint256"], [utilization])))
        rate_calls.append((market_addrs[idx], _GET_BORROW_RATE_SELECTOR + abi_encode(["uint256"], [utilization])))

    if not rate_calls:
        return []

    rate_results = await multicall3_batch(w3, rate_calls, chain_id=chain_id)
    entries: list[LendingRateEntry] = []

    for chunk, idx in enumerate(rate_indices):
        supply_res = rate_results[chunk * 2] if chunk * 2 < len(rate_results) else (False, b"")
        borrow_res = rate_results[chunk * 2 + 1] if chunk * 2 + 1 < len(rate_results) else (False, b"")
        utilization = utilization_values[idx]
        if utilization is None:
            continue
        if not supply_res[0] or not supply_res[1] or not borrow_res[0] or not borrow_res[1]:
            logger.debug("get_lending_rates: compound rate call failed for %s", market_symbols[idx])
            continue

        try:
            supply_rate = int(abi_decode(["uint256"], supply_res[1])[0])
            borrow_rate = int(abi_decode(["uint256"], borrow_res[1])[0])
            entries.append(
                LendingRateEntry(
                    protocol="compound-v3",
                    chain_id=chain_id,
                    market_name=market_names[idx],
                    asset_symbol=market_symbols[idx],
                    supply_apy_pct=_per_second_rate_to_apy_pct(supply_rate),
                    borrow_apy_pct=_per_second_rate_to_apy_pct(borrow_rate),
                    utilization_pct=_utilization_to_pct(utilization),
                )
            )
        except Exception as exc:
            logger.debug("get_lending_rates: failed to decode compound market %s: %s", market_symbols[idx], exc)

    return entries


async def get_lending_rates(
    protocol: Literal["all", "aave-v3", "compound-v3"] = "all",
    chain_id: int = 1,
    assets: list[str] | None = None,
) -> dict[str, Any]:
    """Get current on-chain lending rates for major DeFi protocols."""
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise ValueError(f"Unsupported protocol '{protocol}'. Supported: {', '.join(sorted(_SUPPORTED_PROTOCOLS))}")
    if chain_id not in _SUPPORTED_CHAINS:
        raise ValueError(f"Unsupported chain_id={chain_id}. Supported: {', '.join(str(c) for c in _SUPPORTED_CHAINS)}")
    if assets is not None and len(assets) > _MAX_ASSET_FILTER:
        raise ValueError(f"assets list exceeds {_MAX_ASSET_FILTER}-symbol limit")

    now = time.monotonic()
    key = _cache_key(protocol, chain_id, assets)
    cached = _rates_cache.get(key)
    if cached and now < cached[0]:
        return cached[1]

    assets_filter: set[str] | None = None
    if assets:
        assets_filter = {str(a).strip().upper() for a in assets if str(a).strip()}

    rates: list[LendingRateEntry] = []
    errors: list[str] = []

    w3 = get_web3(chain_id)
    block_task = rpc_call(lambda: w3.eth.block_number, chain_id=chain_id)

    if protocol in {"all", "aave-v3"}:
        try:
            rates.extend(await _fetch_aave_rates(chain_id, assets_filter))
        except Exception as exc:
            logger.warning("get_lending_rates: aave-v3 fetch failed on chain %s: %s", chain_id, exc)
            errors.append("aave-v3 unavailable")

    if protocol in {"all", "compound-v3"}:
        try:
            rates.extend(await _fetch_compound_rates(chain_id, assets_filter))
        except Exception as exc:
            logger.warning("get_lending_rates: compound-v3 fetch failed on chain %s: %s", chain_id, exc)
            errors.append("compound-v3 unavailable")

    try:
        block_number = int(await block_task)
    except Exception as exc:
        logger.warning("get_lending_rates: block number fetch failed on chain %s: %s", chain_id, exc)
        block_number = 0

    rates.sort(key=lambda r: (r.supply_apy_pct, r.borrow_apy_pct), reverse=True)

    result = GetLendingRatesOutput(
        protocol=protocol,
        chain_id=chain_id,
        data_block_number=block_number,
        rates=rates,
        errors=errors,
    ).model_dump()

    _rates_cache[key] = (now + _RATE_CACHE_TTL, result)
    return result


TOOL = ToolDefinition(
    name="get_lending_rates",
    version="1.0.0",
    description=(
        "Get current on-chain lending supply/borrow rates for Aave v3 and Compound v3 on Ethereum or Base. "
        "Returns per-asset APY snapshots and Compound utilization where available. "
        "Useful for protocol-specific stablecoin yield comparisons (e.g., USDC on Aave vs Compound)."
    ),
    tags=["web3", "defi", "lending", "aave", "compound", "yield"],
    input_schema=GetLendingRatesInput,
    output_schema=GetLendingRatesOutput,
    implementation=get_lending_rates,
)
