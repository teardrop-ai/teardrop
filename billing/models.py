# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Billing data models and value helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

# USDC uses 6 decimal places; amounts are stored as atomic BIGINT integers
# (1_000_000 atomic = $1.00). Defined here for documentation/reference; existing
# literals are intentionally left in place to avoid behavioural changes.
USDC_DECIMALS: int = 6

# BillingResult.billing_method values — selects the settlement rail.
BILLING_METHOD_CREDIT = "credit"  # off-chain prepaid credit ledger debit
BILLING_METHOD_X402 = "x402"  # on-chain USDC settlement via x402 facilitator

# BillingResult.scheme values — x402 payment scheme.
BILLING_SCHEME_EXACT = "exact"  # charge the exact signed amount
BILLING_SCHEME_UPTO = "upto"  # charge metered actual_cost_usdc up to a signed ceiling


class PricingRule(BaseModel):
    id: str
    name: str
    run_price_usdc: int  # atomic units (6 decimals), e.g. 10000 = $0.01
    tokens_in_cost_per_1k: int = 0
    tokens_out_cost_per_1k: int = 0
    tool_call_cost: int = 0
    effective_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolPricingOverride(BaseModel):
    tool_name: str
    cost_usdc: int  # atomic USDC, e.g. 15000 = $0.015
    description: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BillingResult(BaseModel):
    """Result of a verify or settle operation, carried through the SSE stream."""

    verified: bool = False
    payment_payload: object | None = None
    payment_requirements: object | None = None
    settled: bool = False
    tx_hash: str = ""
    amount_usdc: int = 0
    error: str = ""
    # Distinguishes on-chain x402 settlement from off-chain credit debit.
    # Set by verify_payment ("x402") or verify_credit ("credit").
    billing_method: str = "x402"
    # Distinguishes exact vs upto within x402. Controls whether settle_payment()
    # passes actual_cost_usdc to the facilitator.
    scheme: str = "exact"


def atomic_usdc_to_price_str(atomic: int) -> str:
    """Convert atomic USDC (6-decimal integer) to an x402 price string.

    Examples:  10000 -> "$0.01",  1000000 -> "$1.00",  500000 -> "$0.50"
    """
    full = f"{atomic / 1_000_000:.6f}"  # e.g. "0.010000"
    integer_part, frac_part = full.split(".")
    stripped = frac_part.rstrip("0")
    # Keep at least 2 decimal places for readability
    if len(stripped) < 2:
        stripped = stripped.ljust(2, "0")
    return f"${integer_part}.{stripped}"
