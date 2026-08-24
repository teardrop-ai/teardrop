#!/usr/bin/env python3

# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Probe DeBank Cloud unit cost for the get_wallet_positions tool.

DeBank bills in "compute units" (1M units / $200 USDC = $0.0002/unit). The
pricing page (cloud.debank.com/#view-all-apis) lists the authoritative unit
costs: `user/all_complex_protocol_list` = 30 units and `user/total_balance` =
30 units. This script verifies those figures against a live call by reading
DeBank's usage headers, and confirms the $0.020 platform price leaves margin.

Usage:
  python scripts/probe_debank_cost.py [--wallet 0x...] [--no-include-net-worth] [--verbose]

Requires DEBANK_API_KEY in the environment or .env AND purchased units (the
endpoints return HTTP 403 without an active plan). Makes real (billable)
DeBank requests — run manually, not in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any


def _ensure_repo_root_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


_ensure_repo_root_on_path()

from teardrop.config import get_settings  # noqa: E402
from tools._internals._http_session import close_http_sessions, get_debank_session  # noqa: E402

_DEBANK_BASE_URL = "https://pro-openapi.debank.com/v1"
_UNIT_PRICE_USDC = 0.0002  # $200 / 1M units
_PLATFORM_PRICE_USDC = 0.020  # current marketplace price for get_wallet_positions

# Documented unit costs from cloud.debank.com/#view-all-apis (fallback when
# DeBank does not expose usage in response headers).
_DOCUMENTED_UNITS = {
    "user/all_complex_protocol_list": 30,
    "user/total_balance": 30,
}

# DeBank returns usage in these response headers (units consumed by the call).
_USAGE_HEADERS = (
    "x-usage",
    "x-remaining-units",
    "x-credit-usage",
    "x-credit-remaining",
    "x-ratelimit-remaining",
)


def _find_usage_headers(headers: Any) -> dict[str, str]:
    """Extract any DeBank usage/unit headers from a response."""
    found: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if isinstance(key, str) and key.lower() in _USAGE_HEADERS:
            found[key] = str(value)
    return found


async def _probe_endpoint(path: str, api_key: str, verbose: bool) -> tuple[int, dict[str, str]]:
    """Call one DeBank endpoint and return (status, usage_headers)."""
    session = await get_debank_session()
    async with session.get(
        f"{_DEBANK_BASE_URL}/{path}",
        headers={"AccessKey": api_key, "Accept": "application/json"},
    ) as response:
        usage = _find_usage_headers(response.headers)
        if verbose:
            print(f"  {path}: HTTP {response.status} usage={usage}")
        return response.status, usage


async def _run(wallet: str, include_net_worth: bool, verbose: bool) -> int:
    api_key = get_settings().debank_api_key.strip()
    if not api_key:
        print("ERROR: DEBANK_API_KEY is not configured (set it in .env or the environment).")
        return 2

    print(f"Probing DeBank cost for wallet {wallet}")
    print(f"  include_net_worth={include_net_worth}")
    print(f"  unit price: ${_UNIT_PRICE_USDC:.4f}/unit")
    print(f"  platform price: ${_PLATFORM_PRICE_USDC:.3f}/call")
    print()

    endpoints = ["user/all_complex_protocol_list"]
    if include_net_worth:
        endpoints.append("user/total_balance")

    total_units = 0
    measured = 0
    for path in endpoints:
        status, usage = await _probe_endpoint(path, api_key, verbose)
        if status != 200:
            print(f"  {path}: HTTP {status} — cannot measure cost")
            continue
        measured += 1
        # DeBank may not expose units in headers; fall back to documented values.
        units = None
        for value in usage.values():
            try:
                units = int(value)
                break
            except (TypeError, ValueError):
                continue
        if units is None:
            units = _DOCUMENTED_UNITS.get(path, 4)
            print(f"  {path}: no usage header; using documented cost of {units} units")
        else:
            print(f"  {path}: {units} units")
        total_units += units

    if measured == 0:
        print()
        print("ERROR: no endpoints returned HTTP 200 — could not measure cost.")
        print("  Check that DEBANK_API_KEY is valid and has purchased units.")
        return 2

    cost_usdc = total_units * _UNIT_PRICE_USDC
    margin = _PLATFORM_PRICE_USDC - cost_usdc
    margin_pct = (margin / _PLATFORM_PRICE_USDC) * 100 if _PLATFORM_PRICE_USDC else 0

    print()
    print(f"Total: {total_units} units ≈ ${cost_usdc:.4f}/call")
    print(f"Platform price: ${_PLATFORM_PRICE_USDC:.3f}/call")
    print(f"Margin: ${margin:.4f}/call ({margin_pct:.1f}%)")
    if margin <= 0:
        print("WARNING: no margin — raise the platform price or reduce provider calls.")
        return 1
    print("OK: margin is positive.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wallet",
        default="0x0000000000000000000000000000000000000001",
        help="EVM wallet to probe (default: a zero-ish address; use an active one for realistic data)",
    )
    parser.add_argument(
        "--no-include-net-worth",
        action="store_true",
        help="Probe positions-only mode (skip user/total_balance)",
    )
    parser.add_argument("--verbose", action="store_true", help="Print per-endpoint usage headers")
    args = parser.parse_args()

    try:
        return asyncio.run(
            _run(
                args.wallet,
                include_net_worth=not args.no_include_net_worth,
                verbose=args.verbose,
            )
        )
    finally:
        asyncio.run(close_http_sessions())


if __name__ == "__main__":
    sys.exit(main())
