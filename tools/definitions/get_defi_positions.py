# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_defi_positions – aggregate DeFi positions across Aave v3, Compound v3, and Uniswap v3 LP.

View-only, hardcoded protocol addresses, chain_id ∈ {1, 8453}. Uses per-protocol
try/except so a failure in one protocol never blocks the others. RPC-heavy fan-outs
are batched through Multicall3 to reduce provider contention and 429s.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._multicall3 import multicall3_batch
from tools.definitions._web3_helpers import get_web3, rpc_call
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── Concurrency bounds ──────────────────────────────────────────────────────
# Uniswap enumeration is capped to prevent runaway calls on whale wallets.
_UNISWAP_MAX_POSITIONS = 20

# Aave total debt / collateral in uint256 sentinel for "no debt / no collateral".
# getUserAccountData returns type(uint256).max for healthFactor when no debt.
_UINT256_MAX = 2**256 - 1
# Defensive threshold: any health factor value above this is treated as "no_debt".
_HEALTH_FACTOR_INFINITE_THRESHOLD = 10**30

# ─── Contract address registry (checksummed, per chain_id) ───────────────────

_AAVE_V3_POOL: dict[int, str] = {
    1: "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
    8453: "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
}

_AAVE_V3_DATA_PROVIDER: dict[int, str] = {
    1: "0x0a16f2FCC0D44FaE41cc54e079281D84A363bECD",
    8453: "0x0F43731EB8d45A581f4a36DD74F5f358bc90C73A",
}

# Curated shortlist of major Aave v3 reserves to fetch per-reserve breakdown for.
# Rest of the wallet's debt/collateral is still captured in the aggregate
# getUserAccountData summary. Addresses are checksummed underlying ERC-20s.
_AAVE_V3_TRACKED_RESERVES: dict[int, list[dict[str, str]]] = {
    1: [
        {"symbol": "WETH", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": "18"},
        {"symbol": "wstETH", "address": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0", "decimals": "18"},
        {"symbol": "WBTC", "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "decimals": "8"},
        {"symbol": "cbBTC", "address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "decimals": "8"},
        {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": "6"},
        {"symbol": "USDT", "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": "6"},
        {"symbol": "DAI", "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F", "decimals": "18"},
        {"symbol": "weETH", "address": "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee", "decimals": "18"},
    ],
    8453: [
        {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": "18"},
        {"symbol": "wstETH", "address": "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "decimals": "18"},
        {"symbol": "cbETH", "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": "18"},
        {"symbol": "cbBTC", "address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "decimals": "8"},
        {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": "6"},
        {"symbol": "USDbC", "address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": "6"},
        {"symbol": "weETH", "address": "0x04C0599Ae5A44757c0af6F9eC3b93da8976c150A", "decimals": "18"},
    ],
}

# Function selector for getUserReserveData(address,address) — keccak256(sig)[0:4].
_GET_USER_RESERVE_DATA_SELECTOR: bytes = bytes(Web3.keccak(text="getUserReserveData(address,address)"))[:4]

# ─── Token symbol lookup (derived from tracked reserves) ─────────────────────
_KNOWN_SYMBOLS: dict[int, dict[str, str]] = {
    chain_id: {r["address"]: r["symbol"] for r in reserves} for chain_id, reserves in _AAVE_V3_TRACKED_RESERVES.items()
}

# Function selectors for frequently batched calls.
_BALANCE_OF_COMET_SELECTOR: bytes = bytes(Web3.keccak(text="balanceOf(address)"))[:4]
_BORROW_BALANCE_SELECTOR: bytes = bytes(Web3.keccak(text="borrowBalanceOf(address)"))[:4]
_IS_LIQUIDATABLE_SELECTOR: bytes = bytes(Web3.keccak(text="isLiquidatable(address)"))[:4]
_USER_COLLATERAL_SELECTOR: bytes = bytes(Web3.keccak(text="userCollateral(address,address)"))[:4]
_TOKEN_OF_OWNER_BY_INDEX_SELECTOR: bytes = bytes(Web3.keccak(text="tokenOfOwnerByIndex(address,uint256)"))[:4]
_POSITIONS_SELECTOR: bytes = bytes(Web3.keccak(text="positions(uint256)"))[:4]

# Compound v3 (Comet) per-market metadata.
# Last reviewed: May 2026.
_COMPOUND_V3_MARKETS: dict[int, list[dict[str, Any]]] = {
    1: [
        {
            "name": "cUSDCv3",
            "address": "0xc3d688B66703497DAA19211EEdff47f25384cdc3",
            "base_symbol": "USDC",
            "base_decimals": "6",
            "collateral_assets": [
                {"symbol": "WBTC", "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "decimals": "8"},
                {"symbol": "COMP", "address": "0xc00e94Cb662C3520282E6f5717214004A7f26888", "decimals": "18"},
                {"symbol": "LINK", "address": "0x514910771AF9Ca656af840dff83E8264EcF986CA", "decimals": "18"},
                {"symbol": "UNI", "address": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", "decimals": "18"},
                {"symbol": "WETH", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": "18"},
            ],
        },
        {
            "name": "cWETHv3",
            "address": "0xA17581A9E3356d9A858b789D68B4d866e593aE94",
            "base_symbol": "WETH",
            "base_decimals": "18",
            "collateral_assets": [
                {"symbol": "wstETH", "address": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0", "decimals": "18"},
                {"symbol": "cbETH", "address": "0xBe9895146f7AF43049ca1c1AE358B0541Ea49704", "decimals": "18"},
                {"symbol": "rETH", "address": "0xae78736Cd615f374D3085123A210448E74Fc6393", "decimals": "18"},
            ],
        },
        {
            "name": "cUSDTv3",
            "address": "0x3Afdc9BCA9213A35503b077a6072F3D0d5AB0840",
            "base_symbol": "USDT",
            "base_decimals": "6",
            "collateral_assets": [
                {"symbol": "WBTC", "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "decimals": "8"},
                {"symbol": "WETH", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": "18"},
            ],
        },
    ],
    8453: [
        {
            "name": "cUSDCv3",
            "address": "0xb125E6687d4313864e53df431d5425969c15Eb2F",
            "base_symbol": "USDC",
            "base_decimals": "6",
            "collateral_assets": [
                {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": "18"},
                {"symbol": "cbETH", "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": "18"},
                {"symbol": "wstETH", "address": "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "decimals": "18"},
                {"symbol": "cbBTC", "address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "decimals": "8"},
                {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": "6"},
            ],
        },
        {
            "name": "cWETHv3",
            "address": "0x46e6b214b524310239732D51387075E0e70970bf",
            "base_symbol": "WETH",
            "base_decimals": "18",
            "collateral_assets": [
                {"symbol": "wstETH", "address": "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "decimals": "18"},
                {"symbol": "cbETH", "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": "18"},
            ],
        },
        {
            "name": "cUSDbCv3",
            "address": "0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf",
            "base_symbol": "USDbC",
            "base_decimals": "6",
            "collateral_assets": [
                {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": "18"},
                {"symbol": "cbETH", "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": "18"},
            ],
        },
    ],
}

_UNISWAP_V3_NFPM: dict[int, str] = {
    1: "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    8453: "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
}

# ─── Minimal view-only ABIs ──────────────────────────────────────────────────

_AAVE_V3_POOL_ABI = [
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

_AAVE_V3_DATA_PROVIDER_ABI = [
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "name": "getUserReserveData",
        "outputs": [
            {"name": "currentATokenBalance", "type": "uint256"},
            {"name": "currentStableDebt", "type": "uint256"},
            {"name": "currentVariableDebt", "type": "uint256"},
            {"name": "principalStableDebt", "type": "uint256"},
            {"name": "scaledVariableDebt", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "stableRateLastUpdated", "type": "uint40"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

_COMET_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "borrowBalanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "asset", "type": "address"},
        ],
        "name": "userCollateral",
        "outputs": [
            {"name": "balance", "type": "uint128"},
            {"name": "_reserved", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "numAssets",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "i", "type": "uint8"}],
        "name": "getAssetInfo",
        "outputs": [
            {
                "components": [
                    {"name": "offset", "type": "uint8"},
                    {"name": "asset", "type": "address"},
                    {"name": "priceFeed", "type": "address"},
                    {"name": "scale", "type": "uint64"},
                    {"name": "borrowCollateralFactor", "type": "uint64"},
                    {"name": "liquidateCollateralFactor", "type": "uint64"},
                    {"name": "liquidationFactor", "type": "uint64"},
                    {"name": "supplyCap", "type": "uint128"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "isLiquidatable",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_UNISWAP_V3_NFPM_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "name": "tokenOfOwnerByIndex",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"name": "nonce", "type": "uint96"},
            {"name": "operator", "type": "address"},
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"name": "tokensOwed0", "type": "uint128"},
            {"name": "tokensOwed1", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


# ─── Schemas ─────────────────────────────────────────────────────────────────


class GetDefiPositionsInput(BaseModel):
    wallet_address: str = Field(..., description="Wallet address (0x…)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class AaveReservePosition(BaseModel):
    symbol: str
    asset_address: str
    supplied_amount: str
    variable_debt_amount: str
    stable_debt_amount: str
    usage_as_collateral: bool


class AavePosition(BaseModel):
    total_collateral_usd: float
    total_debt_usd: float
    available_borrows_usd: float
    ltv_bps: int
    liquidation_threshold_bps: int
    health_factor: float | None
    health_factor_status: str  # "healthy" | "at_risk" | "verified_no_debt"
    reserves: list[AaveReservePosition]


class CompoundCollateral(BaseModel):
    asset_address: str
    amount: str  # raw units (decimals not fetched — included in note)


class CompoundMarketPosition(BaseModel):
    market_name: str
    market_address: str
    base_asset_symbol: str
    base_asset_address: str
    supplied_amount: str
    borrowed_amount: str
    collateral: list[CompoundCollateral]
    is_liquidatable: bool


class UniswapV3Position(BaseModel):
    token_id: str
    token0_address: str
    token0_symbol: str | None = None
    token1_address: str
    token1_symbol: str | None = None
    fee_tier_raw: int
    tick_lower: int
    tick_upper: int
    liquidity: str
    tokens_owed_0: str
    tokens_owed_1: str
    status: str  # "active" | "closed"


class ProtocolErrorInfo(BaseModel):
    protocol: str
    error: str


class GetDefiPositionsOutput(BaseModel):
    wallet_address: str
    chain_id: int
    data_block_number: int
    aave_v3: AavePosition | None = None
    compound_v3: list[CompoundMarketPosition] = Field(default_factory=list)
    uniswap_v3: list[UniswapV3Position] = Field(default_factory=list)
    errors: list[ProtocolErrorInfo] = Field(default_factory=list)
    note: str = (
        "On-chain position snapshot at data_block_number. "
        "Aave USD values are from the Aave oracle (base currency = USD, 8 decimals on Ethereum/Base). "
        "Compound collateral and Uniswap v3 amounts are raw integer units — divide by 10**decimals to format. "
        "Token symbols are resolved for well-known tokens; None means unrecognised — "
        "do NOT call read_contract, web_search, or any external lookup to identify these tokens. "
        "Report them as 'unrecognized token (0x…)' in the final answer and move on. "
        "Uniswap v3 liquidity is returned raw (no underlying token valuation in v1); "
        "tokensOwed0/1 are uncollected fees only. "
        "If a protocol is missing (e.g., aave_v3 is null) and appears in errors, "
        "its risk could not be verified from this snapshot and must not be treated as no debt. "
        "Does not include staking rewards, COMP accruals, or unlisted protocols."
    )


# ─── Protocol fetchers ───────────────────────────────────────────────────────


def _classify_health_factor(raw: int, total_debt_base: int) -> tuple[float | None, str]:
    """Map raw Aave healthFactor uint256 → (float_or_None, status_tag).

    Returns ``verified_no_debt`` only when Aave account data was fetched and
    indicates no debt (or equivalent infinite health factor sentinel).
    """
    if total_debt_base == 0 or raw >= _HEALTH_FACTOR_INFINITE_THRESHOLD or raw == _UINT256_MAX:
        return None, "verified_no_debt"
    hf = raw / 1e18
    status = "healthy" if hf >= 1.0 else "at_risk"
    return hf, status


async def _fetch_aave_v3(w3: Any, wallet: str, chain_id: int) -> AavePosition:
    """Fetch Aave v3 aggregate account data + tracked-reserve breakdown."""
    pool_addr = Web3.to_checksum_address(_AAVE_V3_POOL[chain_id])
    data_provider_addr = Web3.to_checksum_address(_AAVE_V3_DATA_PROVIDER[chain_id])

    pool = w3.eth.contract(address=pool_addr, abi=_AAVE_V3_POOL_ABI)

    # Aggregate account snapshot
    account_data = await rpc_call(lambda: pool.functions.getUserAccountData(wallet).call())
    (total_collateral_base, total_debt_base, available_borrows_base, liq_threshold, ltv, health_factor_raw) = account_data

    hf, hf_status = _classify_health_factor(health_factor_raw, total_debt_base)

    # Per-reserve breakdown via Multicall3 — one RPC call for all tracked reserves.
    # Note: multicall3_batch acquires its own semaphore permit. The outer
    # get_defi_positions holds one permit already; momentarily 2 of 25 are held.
    tracked = _AAVE_V3_TRACKED_RESERVES.get(chain_id, [])
    reserves: list[AaveReservePosition] = []

    if tracked:
        reserve_calls = [
            (
                data_provider_addr,
                _GET_USER_RESERVE_DATA_SELECTOR
                + abi_encode(["address", "address"], [Web3.to_checksum_address(r["address"]), wallet]),
            )
            for r in tracked
        ]
        batch_results = await multicall3_batch(w3, reserve_calls)

        for reserve, (success, return_data) in zip(tracked, batch_results):
            if not success or not return_data:
                logger.debug("Aave reserve %s getUserReserveData reverted", reserve.get("symbol"))
                continue
            try:
                result = abi_decode(
                    [
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint256",
                        "uint40",
                        "bool",
                    ],
                    return_data,
                )
                a_token_balance, stable_debt, variable_debt = result[0], result[1], result[2]
                usage_as_collateral = result[8]
                if a_token_balance == 0 and stable_debt == 0 and variable_debt == 0:
                    continue
                decimals = int(reserve["decimals"])
                divisor = 10**decimals
                reserves.append(
                    AaveReservePosition(
                        symbol=reserve["symbol"],
                        asset_address=Web3.to_checksum_address(reserve["address"]),
                        supplied_amount=f"{a_token_balance / divisor:.6f}",
                        variable_debt_amount=f"{variable_debt / divisor:.6f}",
                        stable_debt_amount=f"{stable_debt / divisor:.6f}",
                        usage_as_collateral=bool(usage_as_collateral),
                    )
                )
            except Exception as exc:
                logger.debug("Aave reserve %s decode failed: %s", reserve.get("symbol"), exc)

    return AavePosition(
        total_collateral_usd=round(total_collateral_base / 1e8, 2),
        total_debt_usd=round(total_debt_base / 1e8, 2),
        available_borrows_usd=round(available_borrows_base / 1e8, 2),
        ltv_bps=int(ltv),
        liquidation_threshold_bps=int(liq_threshold),
        health_factor=hf,
        health_factor_status=hf_status,
        reserves=reserves,
    )


async def _fetch_compound_market(w3: Any, wallet: str, market: dict[str, str]) -> CompoundMarketPosition | None:
    """Fetch a single Compound v3 Comet market position. Returns None if no position."""
    market_addr = Web3.to_checksum_address(market["address"])
    collateral_assets = market.get("collateral_assets", [])

    calls: list[tuple[str, bytes]] = [
        (market_addr, _BALANCE_OF_COMET_SELECTOR + abi_encode(["address"], [wallet])),
        (market_addr, _BORROW_BALANCE_SELECTOR + abi_encode(["address"], [wallet])),
        (market_addr, _IS_LIQUIDATABLE_SELECTOR + abi_encode(["address"], [wallet])),
    ]
    calls.extend(
        [
            (
                market_addr,
                _USER_COLLATERAL_SELECTOR
                + abi_encode(["address", "address"], [wallet, Web3.to_checksum_address(a["address"])]),
            )
            for a in collateral_assets
        ]
    )

    results = await multicall3_batch(w3, calls)

    supplied = 0
    borrowed = 0
    is_liq = False
    if len(results) >= 1 and results[0][0] and results[0][1]:
        supplied = int(abi_decode(["uint256"], results[0][1])[0])
    if len(results) >= 2 and results[1][0] and results[1][1]:
        borrowed = int(abi_decode(["uint256"], results[1][1])[0])
    if len(results) >= 3 and results[2][0] and results[2][1]:
        is_liq = bool(abi_decode(["bool"], results[2][1])[0])

    collateral: list[CompoundCollateral] = []
    for asset, (success, return_data) in zip(collateral_assets, results[3:]):
        if not success or not return_data:
            continue
        try:
            balance = int(abi_decode(["uint128", "uint128"], return_data)[0])
            if balance == 0:
                continue
            collateral.append(
                CompoundCollateral(
                    asset_address=Web3.to_checksum_address(asset["address"]),
                    amount=str(balance),
                )
            )
        except Exception as exc:
            logger.debug("Compound collateral decode failed: %s", exc)

    # Skip market entirely if wallet has no activity here
    if supplied == 0 and borrowed == 0 and not collateral:
        return None

    base_decimals = int(market["base_decimals"])
    base_divisor = 10**base_decimals
    return CompoundMarketPosition(
        market_name=market["name"],
        market_address=market_addr,
        base_asset_symbol=market["base_symbol"],
        # Compound's baseToken is implicit in the market — we carry the symbol/decimals
        # statically to avoid an extra RPC call. The market address is the Comet proxy.
        base_asset_address=market_addr,  # Comet proxy; treat market_address as canonical
        supplied_amount=f"{supplied / base_divisor:.6f}",
        borrowed_amount=f"{borrowed / base_divisor:.6f}",
        collateral=collateral,
        is_liquidatable=is_liq,
    )


async def _fetch_compound_v3(w3: Any, wallet: str, chain_id: int) -> list[CompoundMarketPosition]:
    """Fetch all Compound v3 market positions for a wallet on the given chain."""
    markets = _COMPOUND_V3_MARKETS.get(chain_id, [])

    async def _safe(market: dict[str, str]) -> CompoundMarketPosition | None:
        try:
            return await _fetch_compound_market(w3, wallet, market)
        except Exception as exc:
            logger.debug("Compound market %s fetch failed: %s", market.get("name"), exc)
            return None

    results = await asyncio.gather(*[_safe(m) for m in markets])
    return [r for r in results if r is not None]


async def _fetch_uniswap_v3(w3: Any, wallet: str, chain_id: int) -> list[UniswapV3Position]:
    """Fetch Uniswap v3 LP positions by enumerating NFPM ERC-721 tokens."""
    nfpm_addr = Web3.to_checksum_address(_UNISWAP_V3_NFPM[chain_id])
    nfpm = w3.eth.contract(address=nfpm_addr, abi=_UNISWAP_V3_NFPM_ABI)

    balance = int(await rpc_call(lambda: nfpm.functions.balanceOf(wallet).call()))
    if balance == 0:
        return []

    limit = min(balance, _UNISWAP_MAX_POSITIONS)
    if limit == 0:
        return []

    token_id_calls = [
        (
            nfpm_addr,
            _TOKEN_OF_OWNER_BY_INDEX_SELECTOR + abi_encode(["address", "uint256"], [wallet, i]),
        )
        for i in range(limit)
    ]
    token_id_results = await multicall3_batch(w3, token_id_calls)

    token_ids: list[int] = []
    for idx, (success, return_data) in enumerate(token_id_results):
        if not success or not return_data:
            logger.debug("Uniswap tokenOfOwnerByIndex(%d) reverted", idx)
            continue
        try:
            token_ids.append(int(abi_decode(["uint256"], return_data)[0]))
        except Exception as exc:
            logger.debug("Uniswap tokenOfOwnerByIndex(%d) decode failed: %s", idx, exc)

    if not token_ids:
        return []

    position_calls = [
        (
            nfpm_addr,
            _POSITIONS_SELECTOR + abi_encode(["uint256"], [token_id]),
        )
        for token_id in token_ids
    ]
    position_results = await multicall3_batch(w3, position_calls)

    positions: list[UniswapV3Position] = []
    for token_id, (success, return_data) in zip(token_ids, position_results):
        if not success or not return_data:
            logger.debug("Uniswap positions(%s) reverted", token_id)
            continue
        try:
            result = abi_decode(
                [
                    "uint96",
                    "address",
                    "address",
                    "address",
                    "uint24",
                    "int24",
                    "int24",
                    "uint128",
                    "uint256",
                    "uint256",
                    "uint128",
                    "uint128",
                ],
                return_data,
            )
            (
                _nonce,
                _operator,
                token0,
                token1,
                fee,
                tick_lower,
                tick_upper,
                liquidity,
                _f0,
                _f1,
                tokens_owed_0,
                tokens_owed_1,
            ) = result
            is_closed = int(liquidity) == 0 and int(tokens_owed_0) == 0 and int(tokens_owed_1) == 0
            if is_closed:
                continue
            t0 = Web3.to_checksum_address(token0)
            t1 = Web3.to_checksum_address(token1)
            sym = _KNOWN_SYMBOLS.get(chain_id, {})
            positions.append(
                UniswapV3Position(
                    token_id=str(token_id),
                    token0_address=t0,
                    token0_symbol=sym.get(t0) or t0,
                    token1_address=t1,
                    token1_symbol=sym.get(t1) or t1,
                    fee_tier_raw=int(fee),
                    tick_lower=int(tick_lower),
                    tick_upper=int(tick_upper),
                    liquidity=str(int(liquidity)),
                    tokens_owed_0=str(int(tokens_owed_0)),
                    tokens_owed_1=str(int(tokens_owed_1)),
                    status="active",
                )
            )
        except Exception as exc:
            logger.debug("Uniswap positions(%s) decode failed: %s", token_id, exc)

    return positions


# ─── Entry point ─────────────────────────────────────────────────────────────


async def get_defi_positions(
    wallet_address: str,
    chain_id: int = 1,
) -> dict[str, Any]:
    """Aggregate DeFi positions for a wallet across Aave v3, Compound v3, and Uniswap v3 LP on Ethereum or Base.

    Respects global RPC semaphore to prevent org-level saturation. Per-protocol timeouts (45s)
    prevent hung tasks from blocking others. Per-RPC-call timeouts (15s) prevent individual
    slow RPC requests from hanging.
    """
    if chain_id not in _AAVE_V3_POOL:
        raise ValueError(f"Unsupported chain_id={chain_id}. Supported: 1 (Ethereum), 8453 (Base).")

    wallet = Web3.to_checksum_address(wallet_address)
    w3 = get_web3(chain_id)

    # Launch all three protocol pipelines + block number in parallel
    aave_task = asyncio.create_task(_fetch_aave_v3(w3, wallet, chain_id))
    compound_task = asyncio.create_task(_fetch_compound_v3(w3, wallet, chain_id))
    uniswap_task = asyncio.create_task(_fetch_uniswap_v3(w3, wallet, chain_id))
    block_task = asyncio.create_task(w3.eth.block_number)

    errors: list[ProtocolErrorInfo] = []
    aave_result: AavePosition | None = None
    compound_result: list[CompoundMarketPosition] = []
    uniswap_result: list[UniswapV3Position] = []

    try:
        aave_result = await asyncio.wait_for(aave_task, timeout=45)
    except asyncio.TimeoutError:
        logger.warning("Aave v3 fetch timed out after 45s")
        errors.append(ProtocolErrorInfo(protocol="aave_v3", error="Request timeout (45s)"))
    except Exception as exc:
        logger.warning("Aave v3 fetch failed: %s", exc)
        errors.append(ProtocolErrorInfo(protocol="aave_v3", error=str(exc)[:200]))

    try:
        compound_result = await asyncio.wait_for(compound_task, timeout=45)
    except asyncio.TimeoutError:
        logger.warning("Compound v3 fetch timed out after 45s")
        errors.append(ProtocolErrorInfo(protocol="compound_v3", error="Request timeout (45s)"))
    except Exception as exc:
        logger.warning("Compound v3 fetch failed: %s", exc)
        errors.append(ProtocolErrorInfo(protocol="compound_v3", error=str(exc)[:200]))

    try:
        uniswap_result = await asyncio.wait_for(uniswap_task, timeout=45)
    except asyncio.TimeoutError:
        logger.warning("Uniswap v3 fetch timed out after 45s")
        errors.append(ProtocolErrorInfo(protocol="uniswap_v3", error="Request timeout (45s)"))
    except Exception as exc:
        logger.warning("Uniswap v3 fetch failed: %s", exc)
        errors.append(ProtocolErrorInfo(protocol="uniswap_v3", error=str(exc)[:200]))

    try:
        block_number = int(await block_task)
    except Exception as exc:
        logger.warning("block_number fetch failed: %s", exc)
        block_number = 0

    output = GetDefiPositionsOutput(
        wallet_address=wallet,
        chain_id=chain_id,
        data_block_number=block_number,
        aave_v3=aave_result,
        compound_v3=compound_result,
        uniswap_v3=uniswap_result,
        errors=errors,
    )
    return output.model_dump()


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_defi_positions",
    version="1.0.0",
    description=(
        "Aggregate DeFi positions for a wallet across Aave v3, Compound v3, and Uniswap v3 LP on "
        "Ethereum (chain_id=1) or Base (chain_id=8453). Returns Aave aggregate account health "
        "(collateral, debt, health factor, LTV) with per-reserve breakdown for major assets, "
        "Compound v3 Comet market positions (supply, borrow, per-asset collateral, liquidation flag), "
        "and Uniswap v3 LP positions by token ID (token pair, fee tier, tick range, liquidity, "
        "uncollected fees). Per-protocol failures are isolated — other protocols still return."
    ),
    tags=["web3", "defi", "aave", "compound", "uniswap", "portfolio"],
    input_schema=GetDefiPositionsInput,
    output_schema=GetDefiPositionsOutput,
    implementation=get_defi_positions,
)
