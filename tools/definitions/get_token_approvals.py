# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_token_approvals – ERC-20 allowance audit for a wallet address."""

from __future__ import annotations

import logging
from typing import Any, Literal

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from tools.definitions._multicall3 import multicall3_batch
from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# Approvals >= this threshold are treated as unlimited (~3.4 × 10^38).
# Far above any realistic token supply (e.g., SHIB raw supply ≈ 5.89 × 10^29).
_UNLIMITED_THRESHOLD: int = 2**128

# Uniswap Permit2 singleton — same address on all EVM chains.
# Approval to this address enables off-chain signed permits that are NOT
# visible via standard allowance() queries — flagged separately.
_PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

# ─── Curated spender maps ─────────────────────────────────────────────────────
#
# Well-known DeFi protocol addresses that wallets commonly approve.
# Format: {checksummed_address: "Human-readable name"}
#
# Update via PR when major protocols launch or deprecate routers.
# Last reviewed: April 2026.

_TRUSTED_SPENDERS: dict[int, dict[str, str]] = {
    1: {  # Ethereum mainnet
        # Permit2 (universal across all EVM chains)
        "0x000000000022D473030F116dDEE9F6B43aC78BA3": "Permit2",
        # Uniswap
        "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D": "Uniswap v2 Router",
        "0xE592427A0AEce92De3Edee1F18E0157C05861564": "Uniswap v3 SwapRouter",
        "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45": "Uniswap v3 SwapRouter 02",
        "0xC36442b4a4522E871399CD717aBDD847Ab11FE88": "Uniswap v3 Position Manager",
        "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD": "Uniswap Universal Router v1.2",
        "0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B": "Uniswap Universal Router v1.3",
        # 1inch
        "0x1111111254fb6c44bAC0beD2854e76F90643097d": "1inch v4",
        "0x1111111254EEB25477B68fb85Ed929f73A960582": "1inch v5",
        # 0x Protocol
        "0xDef1C0ded9bec7F1a1670819833240f027b25EfF": "0x Exchange Proxy",
        # Aave
        "0x7d2768dE32b0b80b7a3454c06BdAc94A69DDc7A9": "Aave v2 LendingPool",
        "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2": "Aave v3 Pool",
        # Compound
        "0xc3d688B66703497DAA19211EEdff47f25384cdc3": "Compound v3 cUSDCv3",
        # OpenSea
        "0x00000000000000ADc04C56Bf30aC9d3c0aAF14dC": "OpenSea Seaport 1.5",
    },
    8453: {  # Base
        # Permit2 (same address on all EVM chains)
        "0x000000000022D473030F116dDEE9F6B43aC78BA3": "Permit2",
        # Uniswap
        "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD": "Uniswap Universal Router v1.2",
        "0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B": "Uniswap Universal Router v1.3",
        "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45": "Uniswap v3 SwapRouter 02",
        # Aerodrome
        "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43": "Aerodrome Router",
        # 1inch
        "0x1111111254EEB25477B68fb85Ed929f73A960582": "1inch v5",
        # Aave
        "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5": "Aave v3 Pool",
        # 0x Protocol
        "0xDef1C0ded9bec7F1a1670819833240f027b25EfF": "0x Exchange Proxy",
    },
}

# Function selector for allowance(address,address) — keccak256(sig)[0:4].
_ALLOWANCE_SELECTOR: bytes = bytes(Web3.keccak(text="allowance(address,address)"))[:4]

# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetTokenApprovalsInput(BaseModel):
    wallet_address: str = Field(..., description="Wallet address (0x…)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")
    tokens: list[str] | None = Field(
        default=None,
        description=("Token contract addresses to check (max 50). Defaults to the platform tracked token list."),
    )
    spenders: list[str] | None = Field(
        default=None,
        description=("Spender contract addresses to check (max 20). Defaults to the curated DeFi protocol list."),
    )

    @field_validator("tokens")
    @classmethod
    def _cap_tokens(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) > 50:
            raise ValueError("tokens list exceeds 50-address limit")
        return v

    @field_validator("spenders")
    @classmethod
    def _cap_spenders(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) > 20:
            raise ValueError("spenders list exceeds 20-address limit")
        return v


class ApprovalEntry(BaseModel):
    token_symbol: str | None
    token_address: str
    spender_name: str | None
    spender_address: str
    allowance_raw: str
    allowance_formatted: str
    is_unlimited: bool
    is_permit2: bool
    risk_level: Literal["low", "medium", "high"]


class RiskSummary(BaseModel):
    total_approvals: int
    unlimited_approvals: int
    high_risk_approvals: int
    unknown_spenders: int


class GetTokenApprovalsOutput(BaseModel):
    wallet_address: str
    chain_id: int
    approvals: list[ApprovalEntry]
    risk_summary: RiskSummary
    note: str
    error: str | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _risk_level(is_unlimited: bool, spender_name: str | None) -> Literal["low", "medium", "high"]:
    """Derive risk level from approval metadata.

    high   — unlimited approval granted to an unrecognised spender.
    medium — unlimited approval granted to a known trusted protocol.
    low    — bounded allowance (any spender).
    """
    if is_unlimited and spender_name is None:
        return "high"
    if is_unlimited:
        return "medium"
    return "low"


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_token_approvals(
    wallet_address: str,
    chain_id: int = 1,
    tokens: list[str] | None = None,
    spenders: list[str] | None = None,
) -> dict[str, Any]:
    """Return all non-zero ERC-20 allowances for a wallet with risk annotations."""
    wallet = Web3.to_checksum_address(wallet_address)

    # Import here to avoid package-level circular concerns; safe because
    # get_wallet_portfolio is always loaded alongside this module via __init__.
    from tools.definitions.get_wallet_portfolio import _TRACKED_TOKENS  # noqa: PLC0415

    # ── Resolve token list ───────────────────────────────────────────────────
    if tokens is not None:
        if len(tokens) > 50:
            raise ValueError("tokens list exceeds 50-address limit")
        token_list = [Web3.to_checksum_address(t) for t in tokens]
    else:
        token_list = [t["address"] for t in _TRACKED_TOKENS.get(chain_id, [])]

    # ── Resolve spender list ─────────────────────────────────────────────────
    if spenders is not None:
        if len(spenders) > 20:
            raise ValueError("spenders list exceeds 20-address limit")
        spender_list = [Web3.to_checksum_address(s) for s in spenders]
    else:
        spender_list = list((_TRUSTED_SPENDERS.get(chain_id) or {}).keys())

    if not token_list or not spender_list:
        return {
            "wallet_address": wallet,
            "chain_id": chain_id,
            "approvals": [],
            "risk_summary": {
                "total_approvals": 0,
                "unlimited_approvals": 0,
                "high_risk_approvals": 0,
                "unknown_spenders": 0,
            },
            "note": "No tokens or spenders configured for this chain.",
        }

    w3 = get_web3(chain_id)
    trusted = _TRUSTED_SPENDERS.get(chain_id, {})

    # Symbol lookup from tracked list (best-effort; None for unknown tokens).
    symbol_lookup: dict[str, str] = {t["address"]: t["symbol"] for t in _TRACKED_TOKENS.get(chain_id, [])}

    # Build all (token, spender) pairs and encode each as Multicall3 input.
    # allowance(address owner, address spender) → single uint256.
    pairs = [(t, s) for t in token_list for s in spender_list]
    calls = [
        (token_addr, _ALLOWANCE_SELECTOR + abi_encode(["address", "address"], [wallet, spender_addr]))
        for token_addr, spender_addr in pairs
    ]

    # Submit entire fan-out as a single Multicall3 batch — one RPC call total.
    batch_results = await multicall3_batch(w3, calls, chain_id=chain_id)

    approvals: list[dict[str, Any]] = []
    batch_failed_count = sum(1 for success, return_data in batch_results if not success or not return_data)
    is_total_batch_failure = len(batch_results) > 0 and batch_failed_count == len(batch_results)
    response_error: str | None = None
    if is_total_batch_failure:
        response_error = "Approval data unavailable (RPC batch failed; results may be incomplete)."
        logger.warning(
            "get_token_approvals: multicall batch returned all-failed results (chain_id=%s, wallet=%s, pairs=%d)",
            chain_id,
            wallet,
            len(pairs),
        )

    for (token_addr, spender_addr), (success, return_data) in zip(pairs, batch_results):
        if not success or not return_data:
            logger.debug(
                "allowance(%s, owner=%s, spender=%s): call reverted or empty",
                token_addr,
                wallet,
                spender_addr,
            )
            continue
        try:
            raw: int = abi_decode(["uint256"], return_data)[0]
        except Exception as exc:
            logger.debug("allowance decode failed for %s/%s: %s", token_addr, spender_addr, exc)
            continue
        if raw == 0:
            continue
        is_unlimited = raw >= _UNLIMITED_THRESHOLD
        spender_name = trusted.get(spender_addr)
        approvals.append(
            {
                "token_symbol": symbol_lookup.get(token_addr),
                "token_address": token_addr,
                "spender_name": spender_name,
                "spender_address": spender_addr,
                "allowance_raw": str(raw),
                "allowance_formatted": "unlimited" if is_unlimited else str(raw),
                "is_unlimited": is_unlimited,
                "is_permit2": spender_addr == _PERMIT2_ADDRESS,
                "risk_level": _risk_level(is_unlimited, spender_name),
            }
        )

    # Sort by risk descending so high-risk entries appear first.
    _order = {"high": 0, "medium": 1, "low": 2}
    approvals.sort(key=lambda a: _order[a["risk_level"]])

    unlimited_count = sum(1 for a in approvals if a["is_unlimited"])
    high_risk_count = sum(1 for a in approvals if a["risk_level"] == "high")
    unknown_count = sum(1 for a in approvals if a["spender_name"] is None)

    note = "Non-zero ERC-20 allowances only. Permit2 sub-allowances (off-chain signed permits) are not reflected here."
    if any(a["is_permit2"] for a in approvals):
        note += " Permit2 approval detected — downstream per-token permits are signed off-chain and require separate inspection."

    return {
        "wallet_address": wallet,
        "chain_id": chain_id,
        "approvals": approvals,
        "risk_summary": {
            "total_approvals": len(approvals),
            "unlimited_approvals": unlimited_count,
            "high_risk_approvals": high_risk_count,
            "unknown_spenders": unknown_count,
        },
        "note": note,
        "error": response_error,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_token_approvals",
    version="1.0.0",
    description=(
        "Audit ERC-20 token allowances for a wallet address. Returns all non-zero approvals "
        "across curated DeFi protocol spenders (Uniswap, Aave, Compound, 1inch, 0x, OpenSea). "
        "Flags unlimited approvals with risk levels: high=unknown spender, medium=trusted protocol, "
        "low=bounded amount. Use before swaps to verify approval state, or after security incidents "
        "to detect active exploit vectors. Ethereum mainnet and Base only."
    ),
    tags=["web3", "ethereum", "security", "erc20", "approvals", "defi"],
    input_schema=GetTokenApprovalsInput,
    output_schema=GetTokenApprovalsOutput,
    implementation=get_token_approvals,
)
