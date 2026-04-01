"""get_erc20_balance – fetch ERC-20 token balance for an address."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

# Minimal ERC-20 ABI — balanceOf, symbol, decimals
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


# ─── Schemas ──────────────────────────────────────────────────────────────────

class GetErc20BalanceInput(BaseModel):
    wallet_address: str = Field(..., description="Wallet address (0x…)")
    token_address: str = Field(..., description="ERC-20 token contract address (0x…)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class GetErc20BalanceOutput(BaseModel):
    wallet_address: str
    token_address: str
    token_symbol: str
    token_decimals: int
    balance_raw: str
    balance_formatted: str
    chain_id: int


# ─── Implementation ──────────────────────────────────────────────────────────

async def get_erc20_balance(
    wallet_address: str,
    token_address: str,
    chain_id: int = 1,
) -> dict[str, Any]:
    """Return the ERC-20 token balance with symbol and decimals."""
    w3 = get_web3(chain_id)
    wallet = Web3.to_checksum_address(wallet_address)
    token = Web3.to_checksum_address(token_address)

    contract = w3.eth.contract(address=token, abi=_ERC20_ABI)

    balance_raw, symbol, decimals = await _fetch_token_info(contract, wallet)

    balance_formatted = str(balance_raw / (10**decimals)) if decimals > 0 else str(balance_raw)

    return {
        "wallet_address": wallet,
        "token_address": token,
        "token_symbol": symbol,
        "token_decimals": decimals,
        "balance_raw": str(balance_raw),
        "balance_formatted": balance_formatted,
        "chain_id": chain_id,
    }


async def _fetch_token_info(contract: Any, wallet: str) -> tuple[int, str, int]:
    """Fetch balanceOf, symbol, and decimals concurrently."""
    import asyncio

    balance_task = asyncio.ensure_future(contract.functions.balanceOf(wallet).call())
    symbol_task = asyncio.ensure_future(contract.functions.symbol().call())
    decimals_task = asyncio.ensure_future(contract.functions.decimals().call())

    balance_raw, symbol, decimals = await asyncio.gather(
        balance_task, symbol_task, decimals_task
    )
    return int(balance_raw), str(symbol), int(decimals)


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_erc20_balance",
    version="1.0.0",
    description="Get the ERC-20 token balance of a wallet, including symbol and decimals.",
    tags=["web3", "ethereum", "erc20", "token", "balance"],
    input_schema=GetErc20BalanceInput,
    output_schema=GetErc20BalanceOutput,
    implementation=get_erc20_balance,
)
