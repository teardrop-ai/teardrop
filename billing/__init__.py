# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Billing package facade with compatibility state bridging.

Implementation is split across focused submodules while this package root keeps
legacy import paths and monkeypatch targets stable for tests and callers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar

import asyncpg

import billing.context as _ctx
import billing.history as _history
import billing.pricing as _pricing
import billing.settlement as _settlement
import billing.stripe as _stripe
import billing.x402 as _x402
from billing.credit import BillingCreditService
from billing.delegation import BillingDelegationService
from billing.models import BillingResult, PricingRule, ToolPricingOverride, atomic_usdc_to_price_str
from teardrop.cache import get_redis
from teardrop.config import get_settings

T = TypeVar("T")

# Keep original function refs so wrappers can safely monkeypatch module globals.
_GET_POOL_ORIG = _ctx._get_pool
_GET_DAILY_DEBIT_SPEND_ORIG = _ctx._get_daily_debit_spend
_GET_DAILY_SPEND_CACHE_ORIG = _ctx._get_daily_spend_cache
_GET_ORG_SPENDING_CONFIG_ORIG = _ctx.get_org_spending_config
_UPDATE_ORG_SPENDING_CONFIG_ORIG = _ctx.update_org_spending_config

_GET_CURRENT_PRICING_ORIG = _pricing.get_current_pricing
_GET_CURRENT_PRICING_FOR_MODEL_ORIG = _pricing.get_current_pricing_for_model
_GET_LIVE_PRICING_ORIG = _pricing.get_live_pricing
_GET_LIVE_PRICING_FOR_MODEL_ORIG = _pricing.get_live_pricing_for_model
_GET_CURRENT_TOOL_OVERRIDES_ORIG = _pricing.get_current_tool_overrides
_GET_TOOL_PRICING_OVERRIDES_ORIG = _pricing.get_tool_pricing_overrides
_UPSERT_TOOL_PRICING_OVERRIDE_ORIG = _pricing.upsert_tool_pricing_override
_DELETE_TOOL_PRICING_OVERRIDE_ORIG = _pricing.delete_tool_pricing_override
_RESOLVE_TOOL_COST_ORIG = _pricing.resolve_tool_cost
_CALCULATE_RUN_COST_USDC_ORIG = _pricing.calculate_run_cost_usdc

_INIT_BILLING_ORIG = _x402.init_billing
_CLOSE_BILLING_ORIG = _x402.close_billing
_GET_SERVER_ORIG = _x402._get_server
_GET_PAYMENT_REQUIREMENTS_ORIG = _x402.get_payment_requirements
_BUILD_402_RESPONSE_BODY_ORIG = _x402.build_402_response_body
_BUILD_402_HEADERS_ORIG = _x402.build_402_headers
_REBUILD_REQUIREMENTS_IF_STALE_ORIG = _x402._rebuild_requirements_if_stale
_VERIFY_PAYMENT_ORIG = _x402.verify_payment
_SETTLE_PAYMENT_ORIG = _x402.settle_payment
_CLEANUP_EXPIRED_PAYMENT_NONCES_ORIG = _x402.cleanup_expired_payment_nonces
_BUILD_USDC_TOPUP_REQUIREMENTS_ORIG = _x402.build_usdc_topup_requirements
_VERIFY_AND_SETTLE_USDC_TOPUP_ORIG = _x402.verify_and_settle_usdc_topup
_CREDIT_USDC_TOPUP_ORIG = _x402.credit_usdc_topup
_GET_TREASURY_SIGNER_ORIG = _x402.get_treasury_signer

_RECORD_SETTLEMENT_ORIG = _history.record_settlement
_VERIFY_SETTLEMENT_ON_CHAIN_ORIG = _history.verify_settlement_on_chain
_GET_BILLING_HISTORY_ORIG = _history.get_billing_history
_GET_REVENUE_SUMMARY_ORIG = _history.get_revenue_summary
_GET_INVOICES_ORIG = _history.get_invoices
_GET_INVOICE_BY_RUN_ORIG = _history.get_invoice_by_run

_ENQUEUE_FAILED_SETTLEMENT_ORIG = _settlement.enqueue_failed_settlement
_PROCESS_PENDING_SETTLEMENTS_ORIG = _settlement.process_pending_settlements
_GET_PENDING_SETTLEMENTS_ORIG = _settlement.get_pending_settlements
_RESET_EXHAUSTED_SETTLEMENT_ORIG = _settlement.reset_exhausted_settlement

_CREATE_STRIPE_EMBEDDED_SESSION_ORIG = _stripe.create_stripe_embedded_session
_HANDLE_STRIPE_WEBHOOK_ORIG = _stripe.handle_stripe_webhook
_GET_STRIPE_SESSION_STATUS_ORIG = _stripe.get_stripe_session_status

# Root-level compatibility state patched by tests.
_pool: asyncpg.Pool | None = _ctx._pool
_daily_spend_caches = _ctx._daily_spend_caches

_server = _x402._server
_requirements_cache = _x402._requirements_cache
_exact_requirements_cache = _x402._exact_requirements_cache
_upto_requirements_cache = _x402._upto_requirements_cache
_last_requirements_price_usdc = _x402._last_requirements_price_usdc

_live_pricing_cache = _pricing._live_pricing_cache
_tool_overrides_cache_obj = _pricing._tool_overrides_cache_obj
_model_pricing_cache = _pricing._model_pricing_cache


def _sync_to_modules() -> None:
    """Push root compatibility state and patchable hooks into submodules."""
    _ctx._pool = _pool
    _ctx._daily_spend_caches = _daily_spend_caches
    _ctx._get_pool = _get_pool
    _ctx._get_daily_debit_spend = _get_daily_debit_spend
    _ctx._get_daily_spend_cache = _get_daily_spend_cache

    _pricing._live_pricing_cache = _live_pricing_cache
    _pricing._tool_overrides_cache_obj = _tool_overrides_cache_obj
    _pricing._model_pricing_cache = _model_pricing_cache
    _pricing.get_settings = get_settings
    _pricing.get_redis = get_redis
    _pricing.get_current_pricing_for_model = get_current_pricing_for_model
    _pricing.get_live_pricing = get_live_pricing
    _pricing.get_live_pricing_for_model = get_live_pricing_for_model
    _pricing.get_tool_pricing_overrides = get_tool_pricing_overrides

    _x402._server = _server
    _x402._requirements_cache = _requirements_cache
    _x402._exact_requirements_cache = _exact_requirements_cache
    _x402._upto_requirements_cache = _upto_requirements_cache
    _x402._last_requirements_price_usdc = _last_requirements_price_usdc
    _x402.get_settings = get_settings
    _x402.get_live_pricing = get_live_pricing
    _x402._rebuild_requirements_if_stale = _rebuild_requirements_if_stale

    # Settlement module imported this symbol at module import; keep it patchable.
    _history.record_settlement = record_settlement
    _settlement.record_settlement = record_settlement


def _sync_from_modules() -> None:
    """Pull mutated submodule state back to root compatibility symbols."""
    global _pool, _daily_spend_caches
    global _server, _requirements_cache, _exact_requirements_cache, _upto_requirements_cache, _last_requirements_price_usdc
    global _live_pricing_cache, _tool_overrides_cache_obj, _model_pricing_cache

    _pool = _ctx._pool
    _daily_spend_caches = _ctx._daily_spend_caches

    _server = _x402._server
    _requirements_cache = _x402._requirements_cache
    _exact_requirements_cache = _x402._exact_requirements_cache
    _upto_requirements_cache = _x402._upto_requirements_cache
    _last_requirements_price_usdc = _x402._last_requirements_price_usdc

    _live_pricing_cache = _pricing._live_pricing_cache
    _tool_overrides_cache_obj = _pricing._tool_overrides_cache_obj
    _model_pricing_cache = _pricing._model_pricing_cache


async def _call_async(func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    _sync_to_modules()
    try:
        return await func(*args, **kwargs)
    finally:
        _sync_from_modules()


def _call_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    _sync_to_modules()
    try:
        return func(*args, **kwargs)
    finally:
        _sync_from_modules()


def _get_pool() -> asyncpg.Pool:
    return _call_sync(_GET_POOL_ORIG)


async def _get_daily_debit_spend(executor: asyncpg.Connection | asyncpg.Pool, org_id: str) -> int:
    return await _call_async(_GET_DAILY_DEBIT_SPEND_ORIG, executor, org_id)


def _get_daily_spend_cache(org_id: str):
    return _call_sync(_GET_DAILY_SPEND_CACHE_ORIG, org_id)


def _get_server():
    return _call_sync(_GET_SERVER_ORIG)


async def _rebuild_requirements_if_stale() -> None:
    await _call_async(_REBUILD_REQUIREMENTS_IF_STALE_ORIG)


def _get_credit_service() -> BillingCreditService:
    return BillingCreditService(
        get_pool=_get_pool,
        get_daily_spend_cache=_get_daily_spend_cache,
        get_daily_debit_spend_fn=_get_daily_debit_spend,
        billing_result_factory=BillingResult,
    )


def _get_delegation_service() -> BillingDelegationService:
    return BillingDelegationService(
        get_pool=_get_pool,
        get_settings=get_settings,
        get_daily_debit_spend=_get_daily_debit_spend,
        debit_credit=debit_credit,
        get_live_pricing_for_model=get_live_pricing_for_model,
    )


# Credit API
async def get_credit_balance(org_id: str) -> int:
    """Return the org's prepaid credit-ledger balance in atomic USDC (6 decimals)."""
    return await _get_credit_service().get_credit_balance(org_id)


async def verify_credit(org_id: str, min_balance_usdc: int) -> BillingResult:
    """Check the org has at least ``min_balance_usdc`` credit before a run.

    Sets ``BillingResult.billing_method = "credit"`` so settlement routes to the
    off-chain credit ledger rather than on-chain x402.
    """
    return await _get_credit_service().verify_credit(org_id, min_balance_usdc)


async def debit_credit(org_id: str, amount_usdc: int, reason: str = "") -> tuple[bool, int]:
    """Atomically debit atomic USDC from the org credit ledger.

    Uses ``SELECT FOR UPDATE`` to row-lock ``org_credits`` and enforces both the
    ``is_paused`` flag and the 24h ``spending_limit_usdc`` cap before deducting.
    Writes an append-only ``org_credit_ledger`` debit row. Returns
    ``(True, actual_deducted)`` or ``(False, 0)`` when a guard blocks the debit.
    """
    return await _get_credit_service().debit_credit(org_id, amount_usdc, reason)


async def admin_topup_credit(org_id: str, amount_usdc: int, reason: str = "") -> int:
    """Add atomic USDC to the org credit balance (upsert) and return the new balance.

    Records an append-only ``topup`` ledger row. Used by ``POST /admin/credits/topup``.
    """
    return await _get_credit_service().admin_topup_credit(org_id, amount_usdc, reason)


async def get_credit_history(
    org_id: str,
    operation: str | None = None,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return cursor-paginated ``org_credit_ledger`` rows (debit/topup) newest first."""
    return await _get_credit_service().get_credit_history(org_id, operation, limit, cursor)


# Spending config API
async def get_org_spending_config(org_id: str) -> dict:
    """Return the org's spending config: balance, ``spending_limit_usdc``, pause flag, daily spend."""
    return await _call_async(_GET_ORG_SPENDING_CONFIG_ORIG, org_id)


async def update_org_spending_config(
    org_id: str,
    spending_limit_usdc: int | None = None,
    is_paused: bool | None = None,
) -> dict | None:
    """Update the org's 24h ``spending_limit_usdc`` cap and/or ``is_paused`` flag."""
    return await _call_async(_UPDATE_ORG_SPENDING_CONFIG_ORIG, org_id, spending_limit_usdc, is_paused)


# Delegation API
async def check_delegation_budget(org_id: str, estimated_cost_usdc: int) -> str | None:
    """Validate an A2A delegation against org pause, 24h spend limit, and global cost cap.

    Returns ``None`` when the delegation is allowed, or a human-readable reason string
    when it is blocked.
    """
    return await _get_delegation_service().check_delegation_budget(org_id, estimated_cost_usdc)


def apply_platform_fee(cost_usdc: int) -> int:
    """Add the A2A delegation platform fee (basis points) to an atomic USDC cost."""
    return _get_delegation_service().apply_platform_fee(cost_usdc)


def get_byok_platform_fee(is_byok: bool) -> int:
    """Return the flat BYOK orchestration platform fee in atomic USDC (0 for non-BYOK)."""
    return _get_delegation_service().get_byok_platform_fee(is_byok)


async def calculate_byok_orchestration_cost(
    tokens_in: int,
    tokens_out: int,
    provider: str = "",
    model: str = "",
) -> int:
    """Compute per-token BYOK orchestration cost in atomic USDC for a run's token usage."""
    return await _get_delegation_service().calculate_byok_orchestration_cost(tokens_in, tokens_out, provider, model)


async def fund_delegation(org_id: str, cost_usdc: int, run_id: str, agent_url: str) -> bool:
    """Debit credit for an A2A delegation before forwarding the call. Returns success."""
    return await _get_delegation_service().fund_delegation(org_id, cost_usdc, run_id, agent_url)


async def record_delegation_event(
    org_id: str,
    run_id: str,
    agent_url: str,
    agent_name: str,
    task_status: str,
    cost_usdc: int,
    billing_method: str = "credit",
    settlement_tx: str = "",
    error: str = "",
) -> None:
    """Append an immutable A2A delegation record (cost, status, settlement) for audit."""
    await _get_delegation_service().record_delegation_event(
        org_id,
        run_id,
        agent_url,
        agent_name,
        task_status,
        cost_usdc,
        billing_method,
        settlement_tx,
        error,
    )


async def get_delegation_events(
    org_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return cursor-paginated A2A delegation event history for an org."""
    return await _get_delegation_service().get_delegation_events(org_id, limit, cursor)


# Pricing API
async def get_current_pricing() -> PricingRule | None:
    """Return the active global ``PricingRule`` (run price + per-token rates) in atomic USDC."""
    return await _call_async(_GET_CURRENT_PRICING_ORIG)


async def get_current_pricing_for_model(provider: str, model: str, *, is_byok: bool = False) -> PricingRule | None:
    """Return the effective ``PricingRule`` for a specific provider/model (BYOK-aware)."""
    return await _call_async(_GET_CURRENT_PRICING_FOR_MODEL_ORIG, provider, model, is_byok=is_byok)


async def get_live_pricing() -> PricingRule | None:
    """Return the global pricing rule via the cached live-pricing path."""
    return await _call_async(_GET_LIVE_PRICING_ORIG)


async def get_live_pricing_for_model(provider: str, model: str, *, is_byok: bool = False) -> PricingRule | None:
    """Return cached per-model pricing for a provider/model (BYOK-aware)."""
    return await _call_async(_GET_LIVE_PRICING_FOR_MODEL_ORIG, provider, model, is_byok=is_byok)


async def get_current_tool_overrides() -> dict[str, int]:
    """Return per-tool atomic-USDC price overrides (tool_name -> cost_usdc)."""
    return await _call_async(_GET_CURRENT_TOOL_OVERRIDES_ORIG)


async def get_tool_pricing_overrides() -> dict[str, int]:
    """Return cached per-tool price overrides (tool_name -> atomic USDC)."""
    return await _call_async(_GET_TOOL_PRICING_OVERRIDES_ORIG)


async def upsert_tool_pricing_override(tool_name: str, cost_usdc: int, description: str) -> None:
    """Set a per-org tool price override (atomic USDC); applies to MCP gateway and agent runs."""
    await _call_async(_UPSERT_TOOL_PRICING_OVERRIDE_ORIG, tool_name, cost_usdc, description)


async def delete_tool_pricing_override(tool_name: str) -> bool:
    """Remove a per-tool price override; returns True if a row was deleted."""
    return await _call_async(_DELETE_TOOL_PRICING_OVERRIDE_ORIG, tool_name)


async def resolve_tool_cost(
    tool_name: str,
    overrides: dict[str, int],
    default_cost: int,
    marketplace_enabled: bool,
) -> int:
    """Resolve a tool's per-call cost using precedence: override -> marketplace price -> default."""
    return await _call_async(_RESOLVE_TOOL_COST_ORIG, tool_name, overrides, default_cost, marketplace_enabled)


async def calculate_run_cost_usdc(usage_data: dict, provider: str = "", model: str = "") -> int:
    """Compute total atomic-USDC cost for a run from token usage and tool calls."""
    return await _call_async(_CALCULATE_RUN_COST_USDC_ORIG, usage_data, provider, model)


# x402 API
async def init_billing(pool: asyncpg.Pool) -> None:
    """Initialise billing state (DB pool, x402 facilitator, pricing caches) at startup."""
    await _call_async(_INIT_BILLING_ORIG, pool)


async def close_billing() -> None:
    """Tear down billing resources (x402 client, caches) at shutdown."""
    await _call_async(_CLOSE_BILLING_ORIG)


def get_payment_requirements() -> list:
    """Return the current x402 ``PaymentRequirements`` list for the agent-run price."""
    return _call_sync(_GET_PAYMENT_REQUIREMENTS_ORIG)


def build_402_response_body() -> dict:
    """Build the HTTP 402 body (``accepts`` + ``x402Version``) for the billing gate."""
    return _call_sync(_BUILD_402_RESPONSE_BODY_ORIG)


def build_402_headers() -> dict[str, str]:
    """Build the ``X-PAYMENT-REQUIRED`` response headers for the billing gate."""
    return _call_sync(_BUILD_402_HEADERS_ORIG)


async def verify_payment(payment_header: str) -> BillingResult:
    """Verify a signed x402 payment header (EIP-3009) without settling on-chain.

    Sets ``BillingResult.billing_method = "x402"`` so settlement routes on-chain.
    """
    return await _call_async(_VERIFY_PAYMENT_ORIG, payment_header)


async def settle_payment(
    billing_result: BillingResult,
    actual_cost_usdc: int | None = None,
) -> BillingResult:
    """Settle a verified run. Routes to credit debit when ``billing_method == "credit"``,
    or on-chain x402 settlement when ``"x402"``. For the ``upto`` scheme, ``actual_cost_usdc``
    is passed to the facilitator to charge the metered amount up to the signed ceiling.
    """
    return await _call_async(_SETTLE_PAYMENT_ORIG, billing_result, actual_cost_usdc)


async def cleanup_expired_payment_nonces(retention_hours: int = 24) -> int:
    """Delete x402 payment-nonce replay claims older than ``retention_hours``."""
    return await _call_async(_CLEANUP_EXPIRED_PAYMENT_NONCES_ORIG, retention_hours)


def build_usdc_topup_requirements(amount_usdc: int) -> list:
    """Build x402 ``PaymentRequirements`` for an on-chain USDC credit topup of ``amount_usdc``."""
    return _call_sync(_BUILD_USDC_TOPUP_REQUIREMENTS_ORIG, amount_usdc)


async def verify_and_settle_usdc_topup(
    payment_header: str,
    amount_usdc: int,
) -> BillingResult:
    """Verify and settle an on-chain USDC topup payment for the given atomic amount."""
    return await _call_async(_VERIFY_AND_SETTLE_USDC_TOPUP_ORIG, payment_header, amount_usdc)


async def credit_usdc_topup(org_id: str, amount_usdc: int, tx_hash: str) -> int | None:
    """Credit atomic USDC to an org after a settled on-chain topup.

    Uses ``ON CONFLICT (tx_hash) DO NOTHING`` so a replayed tx cannot double-credit.
    Returns the new balance, or ``None`` when the tx was already processed.
    """
    return await _call_async(_CREDIT_USDC_TOPUP_ORIG, org_id, amount_usdc, tx_hash)


def get_treasury_signer():
    """Return the treasury wallet signer used for on-chain settlement operations."""
    return _call_sync(_GET_TREASURY_SIGNER_ORIG)


# History/invoice API
async def record_settlement(
    usage_event_id: str,
    cost_usdc: int,
    settlement_tx: str,
    settlement_status: str = "settled",
) -> None:
    """Record the settlement outcome (tx hash, status, atomic cost) for a usage event."""
    await _call_async(_RECORD_SETTLEMENT_ORIG, usage_event_id, cost_usdc, settlement_tx, settlement_status)


async def verify_settlement_on_chain(
    usage_event_id: str,
    tx_hash: str,
    chain_id: int,
) -> None:
    """Confirm an x402 settlement tx landed on-chain and reconcile its usage event."""
    await _call_async(_VERIFY_SETTLEMENT_ON_CHAIN_ORIG, usage_event_id, tx_hash, chain_id)


async def get_billing_history(
    user_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return cursor-paginated per-run billing history for a user (atomic USDC costs)."""
    return await _call_async(_GET_BILLING_HISTORY_ORIG, user_id, limit, cursor)


async def get_revenue_summary(
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Return aggregate platform revenue (atomic USDC) over an optional date window."""
    return await _call_async(_GET_REVENUE_SUMMARY_ORIG, start, end)


async def get_invoices(
    user_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return cursor-paginated per-run invoice rows for a user (atomic USDC totals)."""
    return await _call_async(_GET_INVOICES_ORIG, user_id, limit, cursor)


async def get_invoice_by_run(run_id: str, user_id: str) -> dict | None:
    """Return the invoice for a single run, scoped to ``user_id`` (None if not found)."""
    return await _call_async(_GET_INVOICE_BY_RUN_ORIG, run_id, user_id)


# Settlement queue API
async def enqueue_failed_settlement(
    usage_event_id: str,
    org_id: str,
    run_id: str,
    billing_method: str,
    amount_usdc: int,
    payment_payload: str | None = None,
) -> None:
    """Queue a failed settlement for later retry, preserving rail and atomic amount."""
    await _call_async(
        _ENQUEUE_FAILED_SETTLEMENT_ORIG,
        usage_event_id,
        org_id,
        run_id,
        billing_method,
        amount_usdc,
        payment_payload,
    )


async def process_pending_settlements() -> int:
    """Retry queued failed settlements with backoff; returns the count processed."""
    return await _call_async(_PROCESS_PENDING_SETTLEMENTS_ORIG)


async def get_pending_settlements(
    status_filter: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return queued settlement-retry rows, optionally filtered by status."""
    return await _call_async(_GET_PENDING_SETTLEMENTS_ORIG, status_filter, limit)


async def reset_exhausted_settlement(settlement_id: str) -> bool | None:
    """Re-arm a retry-exhausted settlement for another processing attempt."""
    return await _call_async(_RESET_EXHAUSTED_SETTLEMENT_ORIG, settlement_id)


# Stripe API
async def create_stripe_embedded_session(org_id: str, user_id: str, amount_cents: int, return_url: str) -> dict[str, str]:
    """Create a Stripe embedded checkout session for a credit topup (amount in USD cents)."""
    return await _call_async(_CREATE_STRIPE_EMBEDDED_SESSION_ORIG, org_id, user_id, amount_cents, return_url)


async def handle_stripe_webhook(payload: bytes, sig_header: str) -> None:
    """Process a verified ``checkout.session.completed`` Stripe webhook.

    Inserts into ``stripe_webhook_events`` with ``ON CONFLICT (stripe_event_id)
    DO NOTHING`` so duplicate webhook deliveries cannot double-credit the org.
    Raises to signal a retriable failure on transactional errors.
    """
    await _call_async(_HANDLE_STRIPE_WEBHOOK_ORIG, payload, sig_header)


async def get_stripe_session_status(session_id: str, org_id: str) -> dict[str, Any]:
    """Return a Stripe checkout session's status (open/complete/expired), scoped to the org."""
    return await _call_async(_GET_STRIPE_SESSION_STATUS_ORIG, session_id, org_id)


__all__ = [
    # models
    "PricingRule",
    "ToolPricingOverride",
    "BillingResult",
    "atomic_usdc_to_price_str",
    # x402 lifecycle
    "init_billing",
    "close_billing",
    # payment verification and settlement
    "verify_payment",
    "settle_payment",
    "cleanup_expired_payment_nonces",
    "build_402_headers",
    "build_402_response_body",
    "build_usdc_topup_requirements",
    "verify_and_settle_usdc_topup",
    "credit_usdc_topup",
    "get_treasury_signer",
    "get_payment_requirements",
    # pricing
    "get_current_pricing",
    "get_current_pricing_for_model",
    "get_live_pricing",
    "get_live_pricing_for_model",
    "get_current_tool_overrides",
    "get_tool_pricing_overrides",
    "upsert_tool_pricing_override",
    "delete_tool_pricing_override",
    "resolve_tool_cost",
    "calculate_run_cost_usdc",
    # history and invoices
    "record_settlement",
    "verify_settlement_on_chain",
    "get_billing_history",
    "get_revenue_summary",
    "get_invoices",
    "get_invoice_by_run",
    # settlement retry
    "enqueue_failed_settlement",
    "process_pending_settlements",
    "get_pending_settlements",
    "reset_exhausted_settlement",
    # stripe
    "create_stripe_embedded_session",
    "handle_stripe_webhook",
    "get_stripe_session_status",
    # credit
    "verify_credit",
    "debit_credit",
    "admin_topup_credit",
    "get_credit_balance",
    "get_credit_history",
    # delegation
    "check_delegation_budget",
    "apply_platform_fee",
    "get_byok_platform_fee",
    "calculate_byok_orchestration_cost",
    "fund_delegation",
    "record_delegation_event",
    "get_delegation_events",
    # spending limits
    "get_org_spending_config",
    "update_org_spending_config",
    # compatibility internals used by tests/patches
    "_pool",
    "_get_pool",
    "_get_daily_spend_cache",
    "_get_daily_debit_spend",
    "_server",
    "_get_server",
    "_requirements_cache",
    "_exact_requirements_cache",
    "_upto_requirements_cache",
    "_last_requirements_price_usdc",
    "_rebuild_requirements_if_stale",
    "_tool_overrides_cache_obj",
    "_model_pricing_cache",
    "get_redis",
]
