# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Pre-run context assembly and billing gate for ``/agent/run``.

Owns the non-streaming coordination helpers used by the agent router:

* ``_RunContext`` — plain data container populated by ``_prepare_run_context``.
* ``_prepare_run_context`` — concurrent pre-graph IO: org tools, MCP tools,
  marketplace subscriptions, memory recall, LLM config, org name, credit balance.
* ``_record_marketplace_earnings`` — fire-and-forget background earnings for
  marketplace tool calls.
* ``_run_billing_gate`` — auth_method/billing_method dispatch that either
  returns a ``BillingResult`` or a 402 ``JSONResponse`` to short-circuit.

SSE framing and AG-UI stream shaping belong in ``teardrop.agent_stream`` and
the route handler itself remains in ``teardrop.routers.agent``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from agent.graph import get_graph
from billing import (
    BillingResult,
    build_402_headers,
    build_402_response_body,
    get_credit_balance,
    get_current_pricing,
    get_tool_pricing_overrides,
    resolve_tool_cost,
    verify_credit,
    verify_payment,
)
from marketplace import get_marketplace_tool_by_name, record_tool_call_earnings
from mcp_client import build_mcp_langchain_tools
from org_tools import build_org_langchain_tools
from teardrop.config import Settings, get_settings
from teardrop.llm_config import resolve_llm_config
from teardrop.memory import recall_memories
from teardrop.users import get_org_by_id

logger = logging.getLogger(__name__)
settings = get_settings()


class _RunContext:
    """Pre-graph context gathered concurrently for ``/agent/run``.

    Plain attribute container; no behavior. Holds the merged tool set
    (org + MCP + marketplace), recalled memories, resolved LLM config, org
    display name, prepaid credit balance, and the marketplace tool name map
    needed downstream for earnings recording.
    """

    __slots__ = (
        "graph",
        "org_lc_tools",
        "org_tools_by_name",
        "mp_by_name",
        "recalled",
        "llm_config",
        "org_name",
        "credit_balance_usdc",
    )

    def __init__(
        self,
        *,
        graph: Any,
        org_lc_tools: list,
        org_tools_by_name: dict[str, Any],
        mp_by_name: dict[str, Any],
        recalled: list[str],
        llm_config: dict | None,
        org_name: str,
        credit_balance_usdc: int | None,
    ) -> None:
        self.graph = graph
        self.org_lc_tools = org_lc_tools
        self.org_tools_by_name = org_tools_by_name
        self.mp_by_name = mp_by_name
        self.recalled = recalled
        self.llm_config = llm_config
        self.org_name = org_name
        self.credit_balance_usdc = credit_balance_usdc


async def _prepare_run_context(
    *,
    org_id: str,
    user_message: str,
    billing: BillingResult,
    mem_settings: Settings,
) -> _RunContext:
    """Gather pre-graph context concurrently for ``/agent/run``.

    Each helper swallows its own exceptions and returns a safe fallback,
    mirroring the per-call try/except behaviour previously inlined in
    ``_stream()``. This cuts the gap between RUN_STARTED and the first LLM
    token from the sum of all latencies down to the slowest single call.
    """

    async def _safe_mcp_tools() -> tuple[list, dict[str, Any]]:
        try:
            return await build_mcp_langchain_tools(org_id)
        except Exception:
            logger.debug("MCP tool discovery failed for org_id=%s", org_id, exc_info=True)
            return [], {}

    async def _safe_marketplace_tools() -> tuple[list, dict[str, Any]]:
        try:
            from marketplace import build_subscribed_marketplace_tools

            return await build_subscribed_marketplace_tools(org_id)
        except Exception:
            logger.debug("Marketplace subscription injection failed for org_id=%s", org_id, exc_info=True)
            return [], {}

    async def _safe_recall() -> list[str]:
        if not mem_settings.memory_enabled:
            return []
        try:
            entries = await recall_memories(org_id, user_message, mem_settings.memory_top_k)
            return [e.content for e in entries]
        except Exception:
            logger.debug("Memory recall failed for org_id=%s", org_id, exc_info=True)
            return []

    async def _safe_llm_config() -> dict | None:
        try:
            return await resolve_llm_config(org_id)
        except Exception:
            logger.debug("LLM config resolution failed for org_id=%s; using global default", org_id, exc_info=True)
            return None

    async def _safe_org_name() -> str:
        if not org_id:
            return ""
        try:
            _org = await get_org_by_id(org_id)
            return _org.name if _org is not None else ""
        except Exception:
            logger.debug("Org name fetch failed for org_id=%s", org_id, exc_info=True)
            return ""

    async def _safe_credit_balance() -> int | None:
        if not (settings.billing_enabled and billing.verified and billing.billing_method == "credit"):
            return None
        try:
            return await get_credit_balance(org_id)
        except Exception:
            logger.debug("Credit balance fetch failed for org_id=%s", org_id, exc_info=True)
            return None

    (
        graph,
        (org_lc_tools, org_tools_by_name),
        (mcp_tools, mcp_by_name),
        (mp_tools, mp_by_name),
        recalled,
        llm_config,
        org_name,
        credit_balance_usdc,
    ) = await asyncio.gather(
        get_graph(),
        build_org_langchain_tools(org_id),
        _safe_mcp_tools(),
        _safe_marketplace_tools(),
        _safe_recall(),
        _safe_llm_config(),
        _safe_org_name(),
        _safe_credit_balance(),
    )

    # Merge MCP + marketplace tools after gather (cheap dict/list ops).
    if mcp_tools:
        org_lc_tools = list(org_lc_tools) + mcp_tools
        org_tools_by_name = {**org_tools_by_name, **mcp_by_name}
    if mp_tools:
        org_lc_tools = list(org_lc_tools) + mp_tools
        org_tools_by_name = {**org_tools_by_name, **mp_by_name}

    return _RunContext(
        graph=graph,
        org_lc_tools=org_lc_tools,
        org_tools_by_name=org_tools_by_name,
        mp_by_name=mp_by_name,
        recalled=recalled,
        llm_config=llm_config,
        org_name=org_name,
        credit_balance_usdc=credit_balance_usdc,
    )


async def _record_marketplace_earnings(
    *,
    mp_by_name: dict[str, Any],
    tool_names_used: list[str],
    caller_org_id: str,
) -> None:
    """Fire off background tasks recording author earnings for marketplace tool calls.

    Mirrors the inline logic previously embedded in ``_stream()``; failures are
    logged at debug level and never propagate.
    """
    if not (mp_by_name and tool_names_used):
        return
    try:
        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0
        marketplace_enabled = get_settings().marketplace_enabled

        for tname in tool_names_used:
            if tname in mp_by_name and "/" in tname:
                t_slug, t_bare = tname.split("/", 1)
                t_row = await get_marketplace_tool_by_name(t_bare, t_slug)
                if t_row:
                    t_cost = await resolve_tool_cost(tname, overrides, default_cost, marketplace_enabled)
                    author_oid = t_row.get("org_id")
                    if author_oid and t_cost > 0:
                        asyncio.create_task(
                            record_tool_call_earnings(
                                author_org_id=author_oid,
                                caller_org_id=caller_org_id,
                                tool_name=t_bare,
                                total_cost_usdc=t_cost,
                            )
                        )
    except Exception:
        logger.debug("Marketplace earnings recording failed", exc_info=True)


async def _run_billing_gate(
    request: Request,
    payload: dict,
    org_id: str,
    *,
    is_byok: bool,
    platform_fee: int,
) -> tuple[BillingResult, JSONResponse | None]:
    """Pre-run billing gate for ``/agent/run`` (POST /agent/run, agent streaming endpoint).

    Dispatches based on ``auth_method`` from the JWT payload:
      * ``siwe`` with prepaid credit balance > 0
          → org credit rail (``verify_credit``, debit post-run via ``debit_credit``)
      * ``siwe`` with zero credit balance
          → x402 on-chain payment required; returns a ``JSONResponse(402)`` with
            ``X-PAYMENT-REQUIRED`` header so the caller can sign and re-POST
      * ``client_credentials`` / ``email``
          → org prepaid credit rail only (``verify_credit``)
      * billing disabled OR auth_method not in ``billable_auth_methods``
          → returns an unverified ``BillingResult()`` (free pass)

    BYOK orgs use ``platform_fee`` (a flat orchestration fee from
    ``get_byok_platform_fee``) instead of the full LLM passthrough cost. Non-BYOK
    orgs pay the run floor from pricing or ``credit_min_run_reserve_usdc``.

    Returns ``(billing, gate_response)``. ``gate_response`` is non-None only on
    the x402 path, when the request must short-circuit with a 402 the caller
    returns directly. Credit-failure paths raise ``HTTPException(402)`` so callers
    don't need to handle them. ``billing.verified`` may be ``False`` only when
    billing is disabled or auth_method is non-billable; in that case the caller
    proceeds with the default unverified ``BillingResult``.
    """
    auth_method = payload.get("auth_method", "")

    if not (settings.billing_enabled and auth_method in settings.billable_auth_methods):
        return BillingResult(), None

    if auth_method == "siwe":
        # Prefer credit billing when the org has a prepaid balance.
        siwe_credit_balance = await get_credit_balance(org_id)
        if siwe_credit_balance > 0:
            pricing = await get_current_pricing()
            default_min = pricing.run_price_usdc if pricing is not None else 0
            min_required = platform_fee if is_byok else max(default_min, settings.credit_min_run_reserve_usdc)
            billing = await verify_credit(org_id, min_required)
            if not billing.verified:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=billing.error,
                )
            return billing, None

        # No credit balance: require per-request x402 exact payment.
        payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
        if not payment_header:
            return BillingResult(), JSONResponse(
                status_code=402,
                content=build_402_response_body(),
                headers=build_402_headers(),
            )
        billing = await verify_payment(payment_header)
        if not billing.verified:
            return BillingResult(), JSONResponse(
                status_code=402,
                content={"error": billing.error},
                headers=build_402_headers(),
            )
        return billing, None

    # Credit-based billing: ensure org has enough balance to cover at
    # least one run at the current flat-rate floor.
    pricing = await get_current_pricing()
    default_min = pricing.run_price_usdc if pricing is not None else 0
    min_required = platform_fee if is_byok else max(default_min, settings.credit_min_run_reserve_usdc)
    billing = await verify_credit(org_id, min_required)
    if not billing.verified:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=billing.error,
        )
    return billing, None
