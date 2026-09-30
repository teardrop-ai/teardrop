#!/usr/bin/env python3

# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Check that x402 facilitators reject unfunded payers at /verify.

Teardrop releases paid results only after settlement, but it relies on the
facilitator's /verify to reject payers who cannot cover the payment before any
work runs (the x402 exact EVM spec checks balance there). This script signs an
exact payment with a freshly generated, unfunded key and exits non-zero if any
facilitator accepts it. No funds move; /settle is never called.

Usage:
  python scripts/probe_x402_facilitators.py --urls https://facilitator.payai.network https://x402.primer.systems

The CDP facilitator (https://api.cdp.coinbase.com/platform/v2/x402) authenticates with
CDP_API_KEY_ID and CDP_API_KEY_SECRET from the environment; verify calls are not billed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from eth_account import Account
from x402 import ResourceConfig, x402ClientSync, x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmScheme, ExactEvmServerScheme
from x402.schemas.payments import PaymentRequired

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from billing.x402 import build_facilitator_client, verify_with_facilitator  # noqa: E402

_PAY_TO = "0x000000000000000000000000000000000000dEaD"


async def _probe(url: str, network: str, price: str, account) -> tuple[bool, str]:
    try:
        facilitator = build_facilitator_client(url, os.getenv("CDP_API_KEY_ID", ""), os.getenv("CDP_API_KEY_SECRET", ""))
        server = x402ResourceServer(facilitator)
        server.register(network, ExactEvmServerScheme())
        server.initialize()
        requirement = server.build_payment_requirements(
            ResourceConfig(scheme="exact", network=network, pay_to=_PAY_TO, price=price)
        )[0]
    except Exception as exc:  # noqa: BLE001
        return False, f"setup failed ({type(exc).__name__})"

    client = x402ClientSync()
    client.register(network, ExactEvmScheme(signer=account))
    payload = client.create_payment_payload(PaymentRequired(x402_version=2, accepts=[requirement]))
    try:
        result = await verify_with_facilitator(server, payload, requirement)
    except Exception as exc:  # noqa: BLE001
        # Teardrop treats a raised verify as an outage and fails over, so this is non-compliant too.
        return False, f"verify raised ({type(exc).__name__}): {str(exc)[:240]}"

    reason = str(result.invalid_reason or "")
    message = str(result.invalid_message or "")
    if result.is_valid:
        return False, "accepted an unfunded payer"
    # CDP simulates transferWithAuthorization and reports the balance failure as a revert.
    if "insufficient" not in reason.lower() and "revert" not in message.lower():
        return False, f"rejected for another reason: {reason} {message}".rstrip()
    return True, f"rejected unfunded payer: {reason} {message}".rstrip()


async def _main(urls: list[str], network: str, price: str) -> int:
    account = Account.create()
    failures = 0
    for url in urls:
        ok, detail = await _probe(url, network, price, account)
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'} {url}: {detail}")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--urls", nargs="+", required=True, help="Facilitator base URLs, in priority order")
    parser.add_argument("--network", default="eip155:8453", help="x402 network (default: Base mainnet)")
    parser.add_argument("--price", default="$0.001", help="Probe price; keep it at the cheapest tool price")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.urls, args.network, args.price)))


if __name__ == "__main__":
    main()
