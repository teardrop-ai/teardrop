#!/usr/bin/env python3

# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Seed one settled x402 call per paid MCP tool so each gets its own x402 Bazaar listing.

The Bazaar keys MCP listings on ``(resource, toolName)`` and catalogs a tool only after
a payment for it settles through the CDP facilitator, so every paid tool needs one real,
successful call. Free tools never return 402 and cannot be listed; community tools are
credit-only on MCP.

Dry run (default) quotes each tool's 402 without paying. ``--execute`` pays and stops at
the first failure, because unbilled failures count against the gateway's per-IP/per-payer
budget (3 per 10 minutes, then 429).

Usage (from the repo root):
  python -m scripts.seed_bazaar_listings --base-url https://api.teardrop.dev
  $env:TEARDROP_SEED_PRIVATE_KEY = "0x..."
  python -m scripts.seed_bazaar_listings --base-url https://api.teardrop.dev --execute
  python -m scripts.seed_bazaar_listings --base-url https://api.teardrop.dev --execute --tools get_gas_price,get_block
  python -m scripts.seed_bazaar_listings --base-url https://api.teardrop.dev --a2a --execute

MCP-type listings lose serviceName/tags/iconUrl at the CDP facilitator, so ``--a2a`` seeds the HTTP
``/message:send`` listing, which keeps the brand that Agentic.Market shows for the domain.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import requests

PRIVATE_KEY_ENV = "TEARDROP_SEED_PRIVATE_KEY"
DEFAULT_MAX_TOTAL_USDC = 1_000_000  # $1.00 in atomic USDC
A2A_TARGET = "a2a:message:send"
# Trivial task: completes fast and reliably, which is what releases (and settles) the paid result.
A2A_SEED_REQUEST: dict[str, Any] = {
    "message": {"role": "user", "parts": [{"kind": "text", "text": "What is 2 + 2? Reply with only the number."}]},
    "metadata": {"source": "bazaar-seed"},
}
_PAYMENT_RESPONSE_META_KEY = "x402/payment-response"
_GAMMA_TOP_MARKET_URL = (
    "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=1&order=volume24hr&ascending=false"
)

_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # vitalik.eth: long-lived, active, public
_USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
_WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
_TX_HASH = "0xbb4b3fc2b746877dce70862850602f1d19bd890ab4db47e6b7ee1da1fe578a0d"  # first tx of block 20,000,000
_ERC20_TOTAL_SUPPLY_ABI = json.dumps(
    [
        {
            "inputs": [],
            "name": "totalSupply",
            "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        }
    ]
)

# Cheap, valid arguments per paid tool. tests/unit/test_seed_bazaar_listings.py fails when a paid
# tool is missing here, so new tools must add an entry (or an EXCLUDED_TOOLS reason).
TOOL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "assess_counterparty_risk": {"wallet_address": _WALLET, "chain_ids": ["eth"]},
    "convert_currency": {"amount": 100, "from_currency": "USD", "to_currency": "EUR"},
    "decode_transaction": {"tx_hash": _TX_HASH, "chain_id": 1},
    "get_block": {"block_identifier": "latest", "chain_id": 1},
    "get_chain_metrics": {"chains": ["Ethereum", "Base"], "days": 7, "limit": 2},
    "get_defi_positions": {"wallet_address": _WALLET, "chain_id": 1},
    "get_dex_quote": {"token_in": _WETH, "token_out": _USDC, "amount_in": "1000000000000000000", "chain_id": 1},
    "get_dex_volume": {"protocols": ["uniswap-v3"], "lookback_days": 7, "limit": 1},
    "get_erc20_balance": {"wallet_address": _WALLET, "token_address": _USDC, "chain_id": 1},
    "get_eth_balance": {"address": _WALLET, "chain_id": 1},
    "get_gas_price": {"chain_id": 1},
    "get_lending_rates": {"protocol": "aave-v3", "chain_id": 1, "assets": ["USDC"]},
    "get_liquidation_risk": {"wallet_addresses": [_WALLET], "chain_id": 1},
    "get_protocol_tvl": {"protocol": "aave-v3"},
    "get_token_approvals": {"wallet_address": _WALLET, "chain_id": 1, "tokens": [_USDC]},
    "get_token_price": {"tokens": ["ETH"]},
    "get_token_price_historical": {"tokens": ["ETH"], "days": 7, "stats_only": True},
    "get_transaction": {"tx_hash": _TX_HASH, "chain_id": 1},
    "get_wallet_approvals": {"wallet_address": _WALLET, "chain_id": "eth"},
    "get_wallet_history": {"wallet_address": _WALLET, "chain_ids": ["eth"], "page_count": 1},
    "get_wallet_portfolio": {"wallet_address": _WALLET, "chain_id": 1},
    "get_wallet_positions": {"wallet_address": _WALLET, "include_token_balances": False},
    "get_yield_rates": {"protocols": ["aave-v3"], "chain": "Ethereum"},
    "http_fetch": {"url": "https://example.com", "max_chars": 500},
    "read_contract": {
        "contract_address": _USDC,
        "abi_fragment": _ERC20_TOTAL_SUPPLY_ABI,
        "function_name": "totalSupply",
        "chain_id": 1,
    },
    "resolve_ens": {"name": "vitalik.eth"},
    "validate_opportunity": {"pool_id": "747c1d2a-c668-4682-b9f9-296708a3dd90"},  # Lido stETH
    "web_search": {"query": "x402 payments protocol", "num_results": 1},
}


def _top_polymarket_slug() -> dict[str, Any]:
    resp = requests.get(_GAMMA_TOP_MARKET_URL, timeout=15)
    resp.raise_for_status()
    markets = resp.json()
    if not markets or not markets[0].get("slug"):
        raise RuntimeError("Polymarket Gamma returned no active market")
    return {"market": markets[0]["slug"]}


# Arguments that must be fresh at run time (a hardcoded market can be delisted).
DYNAMIC_ARGUMENTS: dict[str, Callable[[], dict[str, Any]]] = {
    "assess_market_resolution": _top_polymarket_slug,
}

# Contains "bot" so the funnel classifies seeding traffic out of the non-bot (organic) stages.
SEED_USER_AGENT = "teardrop-seed-bot/1.0"


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = SEED_USER_AGENT
    return session


EXCLUDED_TOOLS: dict[str, str] = {
    "delegate_to_agent": "spends on a third-party A2A agent; seed manually against a known agent",
    "record_predictions": "internal prediction sink, not a caller-facing tool",
}


@dataclass
class Quote:
    tool: str
    arguments: dict[str, Any]
    amount_usdc: int = 0
    network: str = ""
    body: dict[str, Any] | None = None
    warnings: list[str] | None = None
    error: str = ""


def seedable_tools() -> list[str]:
    """Latest platform tools that answer anonymous MCP calls with an x402 challenge."""
    from teardrop.mcp_gateway import _ANON_FREE_TOOLS
    from tools import registry

    return sorted(
        t.name
        for t in registry.list_latest()
        if t.name not in _ANON_FREE_TOOLS and t.name not in EXCLUDED_TOOLS and "/" not in t.name
    )


def resolve_arguments(tool_name: str) -> dict[str, Any]:
    from tools import registry

    if tool_name in DYNAMIC_ARGUMENTS:
        arguments = DYNAMIC_ARGUMENTS[tool_name]()
    elif tool_name in TOOL_ARGUMENTS:
        arguments = TOOL_ARGUMENTS[tool_name]
    else:
        raise KeyError(f"no seed arguments for '{tool_name}'; add it to TOOL_ARGUMENTS or EXCLUDED_TOOLS")
    tool = registry.get(tool_name)
    if tool is None:
        raise KeyError(f"'{tool_name}' is not a registered tool")
    # Validating locally avoids paying (and burning failure budget) on arguments the tool would reject.
    tool.input_schema.model_validate(arguments)
    return arguments


def _tools_call(tool_name: str, arguments: dict[str, Any], req_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}


def _declares_output(tool_name: str) -> bool:
    """Whether the gateway should declare Bazaar output for this tool (mirrors teardrop.mcp_gateway)."""
    from teardrop.mcp_gateway import _bazaar_output
    from tools import registry

    tool = registry.get(tool_name)
    return tool is not None and _bazaar_output(tool.name, tool.version) is not None


def quote_tool(session: requests.Session, base_url: str, tool_name: str, arguments: dict[str, Any]) -> Quote:
    """Fetch the unpaid 402 for a tool and check it carries per-tool Bazaar metadata."""
    quote = Quote(tool=tool_name, arguments=arguments, warnings=[])
    resp = session.post(
        f"{base_url}/tools/mcp",
        json=_tools_call(tool_name, arguments, 1),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    body = _read_challenge(quote, resp)
    if body is None:
        return quote
    bazaar = (body.get("extensions") or {}).get("bazaar") or {}
    bazaar_input = bazaar.get("info", {}).get("input", {})
    if bazaar_input.get("toolName") != tool_name:
        quote.warnings.append("402 lacks per-tool Bazaar toolName")
    if _declares_output(tool_name) and not bazaar.get("info", {}).get("output"):
        quote.warnings.append("402 lacks Bazaar output declaration (gateway not redeployed?)")
    return quote


def quote_a2a(session: requests.Session, base_url: str) -> Quote:
    """Fetch the unpaid ``/message:send`` 402: the only paid HTTP surface, so the only one whose brand reaches catalogs."""
    quote = Quote(tool=A2A_TARGET, arguments=A2A_SEED_REQUEST, warnings=[])
    resp = session.post(f"{base_url}/message:send", json=A2A_SEED_REQUEST, headers={"Accept": "application/json"}, timeout=30)
    body = _read_challenge(quote, resp)
    if body is not None:
        bazaar_input = ((body.get("extensions") or {}).get("bazaar") or {}).get("info", {}).get("input", {})
        if bazaar_input.get("type") != "http":
            quote.warnings.append("402 lacks an HTTP Bazaar declaration")
    return quote


def _read_challenge(quote: Quote, resp: Any) -> dict[str, Any] | None:
    """Fill price/network from a 402 and warn on missing brand metadata; None (with ``quote.error``) otherwise."""
    if resp.status_code != 402:
        quote.error = f"expected 402, got {resp.status_code}: {resp.text[:200]}"
        return None
    body = resp.json()
    exact = [req for req in body.get("accepts", []) if req.get("scheme", "exact") == "exact"]
    if not exact:
        quote.error = "402 offers no exact payment requirement"
        return None
    quote.body = {**body, "accepts": exact[:1]}
    quote.amount_usdc = int(exact[0]["amount"])
    quote.network = exact[0].get("network", "")
    resource = body.get("resource") or {}
    if not resource.get("serviceName") or not resource.get("tags") or not resource.get("iconUrl"):
        quote.warnings.append("402 resource lacks serviceName/tags/iconUrl (not redeployed?)")
    return body


def sign_payment(payment_required_body: dict[str, Any], private_key: str) -> str:
    """Sign the quoted exact requirement (EIP-3009) and encode it for the X-PAYMENT header."""
    from eth_account import Account
    from x402 import parse_payment_required, x402ClientSync
    from x402.mechanisms.evm.exact import ExactEvmScheme

    payment_required = parse_payment_required(payment_required_body)
    client = x402ClientSync()
    client.register(payment_required.accepts[0].network, ExactEvmScheme(signer=Account.from_key(private_key)))
    payload = client.create_payment_payload(payment_required)
    return base64.b64encode(payload.model_dump_json().encode()).decode()


def pay_tool(session: requests.Session, base_url: str, quote: Quote, payment_header: str) -> str:
    """Execute the paid call; return the settlement transaction hash or raise."""
    resp = session.post(
        f"{base_url}/tools/mcp",
        json=_tools_call(quote.tool, quote.arguments, 2),
        headers={"Accept": "application/json", "X-PAYMENT": payment_header},
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"paid call returned {resp.status_code}: {resp.text[:300]}")
    result = resp.json().get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"tool returned isError (unsettled): {json.dumps(result.get('content'))[:300]}")
    tx_hash = _receipt_transaction(resp.headers, result)
    if not tx_hash:
        # Stop rather than guess: the payment may still have settled, so check the chain before re-running.
        raise RuntimeError("tool ran but no x402 settlement receipt was found; check the payer's transactions")
    return tx_hash


def pay_a2a(session: requests.Session, base_url: str, quote: Quote, payment_header: str) -> str:
    """Run one paid A2A task; the result is released (and the receipt sent) only after settlement."""
    resp = session.post(
        f"{base_url}/message:send",
        json=quote.arguments,
        headers={"Accept": "application/json", "X-PAYMENT": payment_header},
        timeout=300,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"paid A2A task returned {resp.status_code}: {resp.text[:300]}")
    tx_hash = _receipt_transaction(resp.headers, {})
    if not tx_hash:
        raise RuntimeError("A2A task finished without a settlement receipt; check the payer's transactions")
    return tx_hash


def _receipt_transaction(headers: Any, result: dict[str, Any]) -> str:
    """Settlement tx hash: the PAYMENT-RESPONSE header (X-PAYMENT callers) or ``_meta`` (``_meta`` payers)."""
    encoded = headers.get("payment-response") or headers.get("x-payment-response")
    if encoded:
        from x402.http import decode_payment_response_header

        return decode_payment_response_header(encoded).transaction or ""
    receipt = (result.get("_meta") or {}).get(_PAYMENT_RESPONSE_META_KEY) or {}
    return receipt.get("transaction") or ""


def _select(requested: Iterable[str] | None, skipped: Iterable[str]) -> list[str]:
    available = seedable_tools()
    chosen = list(requested) if requested else available
    unknown = sorted(set(chosen) - set(available))
    if unknown:
        raise SystemExit(f"not seedable (free, excluded, or unregistered): {', '.join(unknown)}")
    skip = set(skipped)
    return [name for name in chosen if name not in skip]


def _usd(atomic: int) -> str:
    return f"${atomic / 1_000_000:.4f}"


def run(
    base_url: str,
    tools: list[str],
    *,
    execute: bool,
    private_key: str | None,
    max_total_usdc: int,
    delay_seconds: float,
    allow_warnings: bool = False,
    session: requests.Session | None = None,
) -> int:
    session = session or _new_session()
    quotes: list[Quote] = []
    for name in tools:
        try:
            quote = (
                quote_a2a(session, base_url)
                if name == A2A_TARGET
                else quote_tool(session, base_url, name, resolve_arguments(name))
            )
        except Exception as exc:  # noqa: BLE001 - report every tool before deciding
            quote = Quote(tool=name, arguments={}, error=f"{type(exc).__name__}: {exc}")
        quotes.append(quote)
        status = f"ERROR {quote.error}" if quote.error else f"{_usd(quote.amount_usdc)} on {quote.network}"
        print(f"  {name:<28} {status}")
        for warning in quote.warnings or []:
            print(f"  {'':<28} warn: {warning}")

    failed = [q for q in quotes if q.error]
    total = sum(q.amount_usdc for q in quotes if not q.error)
    print(f"\n{len(quotes) - len(failed)} quotable tools, total {_usd(total)}; {len(failed)} errors")
    if failed:
        print("Fix quote errors before paying.")
        return 1
    if not execute:
        print("Dry run only; re-run with --execute to pay.")
        return 0
    if any(q.warnings for q in quotes) and not allow_warnings:
        # The Bazaar records metadata at settlement; paying now would index incomplete listings.
        print("Quotes have warnings; deploy the fix first or pass --allow-warnings.")
        return 1
    if not private_key:
        print(f"--execute requires {PRIVATE_KEY_ENV}.")
        return 1
    if total > max_total_usdc:
        print(f"Total {_usd(total)} exceeds --max-total-usd {_usd(max_total_usdc)}; aborting.")
        return 1

    for index, quote in enumerate(quotes):
        if index:
            time.sleep(delay_seconds)
        try:
            pay = pay_a2a if quote.tool == A2A_TARGET else pay_tool
            tx_hash = pay(session, base_url, quote, sign_payment(quote.body or {}, private_key))
        except Exception as exc:  # noqa: BLE001
            # Stop: each further unbilled failure burns the 3-per-10-minute failure budget.
            print(f"  {quote.tool:<28} FAILED {exc}")
            print(f"Stopped after {index} settled call(s); fix and re-run with --tools.")
            return 1
        print(f"  {quote.tool:<28} settled {_usd(quote.amount_usdc)} tx {tx_hash}")
    print(f"\nSettled {len(quotes)} tool(s). Bazaar indexing is asynchronous; check listings later.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="Teardrop API base URL, e.g. https://api.teardrop.dev")
    parser.add_argument("--execute", action="store_true", help=f"Pay for each call (key from {PRIVATE_KEY_ENV})")
    parser.add_argument("--tools", default="", help="Comma-separated subset of tools to seed (default: all)")
    parser.add_argument("--skip", default="", help="Comma-separated tools to skip")
    parser.add_argument(
        "--max-total-usd",
        type=float,
        default=DEFAULT_MAX_TOTAL_USDC / 1_000_000,
        help="Abort --execute if the quoted total exceeds this many USD (default: 1.00)",
    )
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between paid calls (default: 2)")
    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="Pay even if a 402 lacks Bazaar metadata (indexes an incomplete listing)",
    )
    parser.add_argument("--list", action="store_true", help="Print seedable and excluded tools, then exit")
    parser.add_argument(
        "--a2a",
        action="store_true",
        help="Seed the HTTP /message:send listing instead of MCP tools (carries the brand name and icon)",
    )
    return parser


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        print("\n".join(seedable_tools()))
        for name, reason in EXCLUDED_TOOLS.items():
            print(f"(excluded) {name}: {reason}")
        return 0
    tools = [A2A_TARGET] if args.a2a else _select(_csv(args.tools) or None, _csv(args.skip))
    base_url = args.base_url.rstrip("/")
    target = f"{base_url}/message:send" if args.a2a else f"{base_url}/tools/mcp"
    print(f"{'Paying' if args.execute else 'Quoting'} {len(tools)} target(s) at {target}\n")
    return run(
        base_url,
        tools,
        execute=args.execute,
        private_key=os.environ.get(PRIVATE_KEY_ENV, "").strip() or None,
        max_total_usdc=round(args.max_total_usd * 1_000_000),
        delay_seconds=args.delay,
        allow_warnings=args.allow_warnings,
    )


if __name__ == "__main__":
    sys.exit(main())
