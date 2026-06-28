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
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage

from agent.graph import get_graph
from agent.state import AgentState
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
from marketplace import get_marketplace_tool_by_name, record_marketplace_tool_usage_many, record_tool_call_earnings
from mcp_client import build_mcp_langchain_tools
from org_tools import build_org_langchain_tools
from teardrop.agent_event_loop import _coerce_stream_text
from teardrop.agent_post_run import calculate_run_cost, dispatch_settlement, fetch_usage_snapshot
from teardrop.config import Settings, get_settings
from teardrop.llm_config import resolve_llm_config
from teardrop.memory import recall_memories
from teardrop.public_url import public_base_url
from teardrop.usage import UsageEvent, record_usage_event
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


@dataclass(slots=True)
class AgentRunOnceResult:
    task_state: str
    response_state: str
    output_text: str
    duration_ms: int
    usage_event: UsageEvent
    usage_data: dict[str, Any]
    llm_config: dict[str, Any] | None
    marketplace_stats_billable: bool


def _usage_metadata_template() -> dict[str, Any]:
    return {
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "tool_calls": 0,
        "tool_names": [],
        "billable_tool_calls": 0,
        "billable_tool_names": [],
        "failed_tool_calls": 0,
        "failed_tool_names": [],
    }


def _snapshot_values(snapshot_or_state: Any) -> dict[str, Any]:
    if snapshot_or_state is None:
        return {}
    if isinstance(snapshot_or_state, dict):
        return snapshot_or_state
    if hasattr(snapshot_or_state, "values") and isinstance(snapshot_or_state.values, dict):
        return snapshot_or_state.values
    if hasattr(snapshot_or_state, "model_dump"):
        dumped = snapshot_or_state.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _extract_final_agent_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if getattr(message, "type", "") != "ai":
            continue
        text = _coerce_stream_text(getattr(message, "content", "")).strip()
        if text:
            return text
    for message in reversed(messages):
        text = _coerce_stream_text(getattr(message, "content", "")).strip()
        if text:
            return text
    return ""


async def record_failure_usage_event(
    *,
    graph: Any,
    config: dict[str, Any],
    run_id: str,
    thread_id: str,
    usage_user_id: str,
    usage_org_id: str,
    duration_ms: int,
    llm_config: dict[str, Any] | None,
    platform_fee: int,
    runtime_settings: Settings,
) -> UsageEvent:
    _, usage_data = await fetch_usage_snapshot(
        graph=graph,
        config=config,
        run_id=run_id,
        settings=runtime_settings,
    )
    usage_event = UsageEvent(
        user_id=usage_user_id,
        org_id=usage_org_id,
        thread_id=thread_id,
        run_id=run_id,
        tokens_in=usage_data.get("tokens_in", 0),
        tokens_out=usage_data.get("tokens_out", 0),
        cache_read_tokens=usage_data.get("cache_read_tokens", 0),
        cache_creation_tokens=usage_data.get("cache_creation_tokens", 0),
        tool_calls=usage_data.get("tool_calls", 0),
        tool_names=usage_data.get("tool_names", []),
        billable_tool_calls=usage_data.get("billable_tool_calls", usage_data.get("tool_calls", 0)),
        billable_tool_names=usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
        failed_tool_calls=usage_data.get("failed_tool_calls", 0),
        failed_tool_names=usage_data.get("failed_tool_names", []),
        duration_ms=duration_ms,
        cost_usdc=0,
        platform_fee_usdc=0 if platform_fee > 0 else 0,
        provider=llm_config["provider"] if llm_config else runtime_settings.agent_provider,
        model=llm_config["model"] if llm_config else runtime_settings.agent_model,
    )
    await record_usage_event(usage_event)
    return usage_event


async def run_agent_once(
    *,
    org_id: str,
    user_id: str,
    usage_user_id: str,
    usage_org_id: str,
    user_message: str,
    run_id: str,
    thread_id: str,
    billing: BillingResult,
    is_byok: bool,
    org_llm_cfg: Any,
    platform_fee: int,
    timeout_seconds: float,
    metadata: dict[str, Any] | None = None,
    excluded_tool_names: list[str] | None = None,
    user_role: str = "user",
    user_wallet_address: str | None = None,
    jwt_token: str | None = None,
    emit_ui: bool = False,
) -> AgentRunOnceResult:
    runtime_settings = get_settings()
    start_time = time.monotonic()
    ctx = await _prepare_run_context(
        org_id=org_id,
        user_message=user_message,
        billing=billing,
        mem_settings=runtime_settings,
    )

    initial_state = AgentState(
        messages=[HumanMessage(content=user_message)],
        metadata={
            **(metadata or {}),
            "thread_id": thread_id,
            "run_id": run_id,
            "user_id": user_id,
            "org_id": org_id,
            "_usage": _usage_metadata_template(),
            "_excluded_tool_names": list(excluded_tool_names or []),
            "_memories": ctx.recalled,
            "_llm_config": ctx.llm_config,
            "_org_name": ctx.org_name,
            "_user_role": user_role,
            "_user_wallet_address": user_wallet_address,
            "_credit_balance_usdc": ctx.credit_balance_usdc,
            "_jwt_token": jwt_token,
            "emit_ui": emit_ui,
        },
    )
    config = {
        "configurable": {
            "thread_id": thread_id,
            "_org_tools": ctx.org_lc_tools,
            "_org_tools_by_name": ctx.org_tools_by_name,
        }
    }

    invoke_result: Any = None
    try:
        invoke_result = await asyncio.wait_for(
            ctx.graph.ainvoke(initial_state, config),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        usage_event = await record_failure_usage_event(
            graph=ctx.graph,
            config=config,
            run_id=run_id,
            thread_id=thread_id,
            usage_user_id=usage_user_id,
            usage_org_id=usage_org_id,
            duration_ms=duration_ms,
            llm_config=ctx.llm_config,
            platform_fee=platform_fee,
            runtime_settings=runtime_settings,
        )
        return AgentRunOnceResult(
            task_state="timeout",
            response_state="failed",
            output_text="Task timed out.",
            duration_ms=duration_ms,
            usage_event=usage_event,
            usage_data={},
            llm_config=ctx.llm_config,
            marketplace_stats_billable=False,
        )
    except Exception:
        logger.exception("run_agent_once failed run_id=%s", run_id)
        duration_ms = int((time.monotonic() - start_time) * 1000)
        usage_event = await record_failure_usage_event(
            graph=ctx.graph,
            config=config,
            run_id=run_id,
            thread_id=thread_id,
            usage_user_id=usage_user_id,
            usage_org_id=usage_org_id,
            duration_ms=duration_ms,
            llm_config=ctx.llm_config,
            platform_fee=platform_fee,
            runtime_settings=runtime_settings,
        )
        return AgentRunOnceResult(
            task_state="failed",
            response_state="failed",
            output_text="Task failed.",
            duration_ms=duration_ms,
            usage_event=usage_event,
            usage_data={},
            llm_config=ctx.llm_config,
            marketplace_stats_billable=False,
        )

    duration_ms = int((time.monotonic() - start_time) * 1000)
    state_snapshot, usage_data = await fetch_usage_snapshot(
        graph=ctx.graph,
        config=config,
        run_id=run_id,
        settings=runtime_settings,
    )
    values = _snapshot_values(state_snapshot) or _snapshot_values(invoke_result)
    messages = values.get("messages", []) if isinstance(values, dict) else []
    output_text = _extract_final_agent_text(messages)
    task_status_value = str(values.get("task_status", "completed")).lower()
    task_state = "failed" if task_status_value.endswith("failed") else "completed"
    if not output_text:
        output_text = "Task failed." if task_state == "failed" else "Task completed."

    cost_usdc = await calculate_run_cost(
        usage_data=usage_data,
        llm_config=ctx.llm_config,
        settings=runtime_settings,
    )

    usage_event = UsageEvent(
        user_id=usage_user_id,
        org_id=usage_org_id,
        thread_id=thread_id,
        run_id=run_id,
        tokens_in=usage_data.get("tokens_in", 0),
        tokens_out=usage_data.get("tokens_out", 0),
        cache_read_tokens=usage_data.get("cache_read_tokens", 0),
        cache_creation_tokens=usage_data.get("cache_creation_tokens", 0),
        tool_calls=usage_data.get("tool_calls", 0),
        tool_names=usage_data.get("tool_names", []),
        billable_tool_calls=usage_data.get("billable_tool_calls", usage_data.get("tool_calls", 0)),
        billable_tool_names=usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
        failed_tool_calls=usage_data.get("failed_tool_calls", 0),
        failed_tool_names=usage_data.get("failed_tool_names", []),
        duration_ms=duration_ms,
        cost_usdc=cost_usdc,
        platform_fee_usdc=platform_fee,
        provider=ctx.llm_config["provider"] if ctx.llm_config else runtime_settings.agent_provider,
        model=ctx.llm_config["model"] if ctx.llm_config else runtime_settings.agent_model,
    )
    await record_usage_event(usage_event)

    settlement_result: dict[str, Any] = {}
    delegation_spend = usage_data.get("delegation_spend_usdc", 0)
    async for _ignored in dispatch_settlement(
        billing=billing,
        is_byok=is_byok,
        settings=runtime_settings,
        org_llm_cfg=org_llm_cfg,
        usage_data=usage_data,
        usage_event=usage_event,
        platform_fee=platform_fee,
        cost_usdc=cost_usdc,
        delegation_spend=delegation_spend,
        org_id=org_id,
        run_id=run_id,
        result=settlement_result,
    ):
        pass

    marketplace_stats_billable = settlement_result.get("marketplace_stats_billable", False)
    if marketplace_stats_billable:
        await _record_marketplace_earnings(
            mp_by_name=ctx.mp_by_name,
            tool_names_used=usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
            caller_org_id=org_id,
        )
        billable_tool_names = usage_data.get("billable_tool_names", usage_data.get("tool_names", []))
        if isinstance(billable_tool_names, list):
            asyncio.create_task(record_marketplace_tool_usage_many([str(name) for name in billable_tool_names]))

    return AgentRunOnceResult(
        task_state=task_state,
        response_state=task_state,
        output_text=output_text,
        duration_ms=duration_ms,
        usage_event=usage_event,
        usage_data=usage_data,
        llm_config=ctx.llm_config,
        marketplace_stats_billable=marketplace_stats_billable,
    )


def _agent_run_402_resource(request: Request) -> dict[str, str]:
    base_url = public_base_url(request, settings)
    return {
        "url": f"{base_url}/agent/run",
        "description": "AG-UI streaming agent run endpoint.",
        "mimeType": "text/event-stream",
    }


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

    async def _safe_org_tools() -> tuple[list, dict[str, Any]]:
        try:
            return await build_org_langchain_tools(org_id)
        except Exception:
            logger.warning("Org tool discovery failed for org_id=%s", org_id, exc_info=True)
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
        _safe_org_tools(),
        _safe_mcp_tools(),
        _safe_marketplace_tools(),
        _safe_recall(),
        _safe_llm_config(),
        _safe_org_name(),
        _safe_credit_balance(),
    )

    # Merge MCP + marketplace tools after gather (cheap dict/list ops).
    _wh_count = len(org_lc_tools)
    _mcp_count = len(mcp_tools)
    _mp_count = len(mp_tools)
    if mcp_tools:
        org_lc_tools = list(org_lc_tools) + mcp_tools
        org_tools_by_name = {**org_tools_by_name, **mcp_by_name}
    if mp_tools:
        org_lc_tools = list(org_lc_tools) + mp_tools
        org_tools_by_name = {**org_tools_by_name, **mp_by_name}

    # Telemetry: per-org tool inventory snapshot.
    logger.info(
        "tool_inventory org_id=%s webhook=%d mcp=%d marketplace=%d total=%d",
        org_id,
        _wh_count,
        _mcp_count,
        _mp_count,
        len(org_lc_tools),
    )

    # Always-on discovery diagnostic: makes "missing org tool" runs deterministic.
    # If this line is absent from logs, the patched code is not the code running.
    logger.debug(
        "prepare_run_context: org tool discovery org_id=%s resolved_count=%d names=%s",
        org_id,
        len(org_lc_tools),
        sorted(org_tools_by_name.keys()),
    )

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
                        ``PAYMENT-REQUIRED`` plus the legacy ``X-PAYMENT-REQUIRED`` alias
                        so the caller can sign and re-POST
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
        response_kwargs = {"resource": _agent_run_402_resource(request)}
        if not payment_header:
            return BillingResult(), JSONResponse(
                status_code=402,
                content=build_402_response_body(**response_kwargs),
                headers=build_402_headers(**response_kwargs),
            )
        billing = await verify_payment(payment_header)
        if not billing.verified:
            response_kwargs["error"] = billing.error
            return BillingResult(), JSONResponse(
                status_code=402,
                content=build_402_response_body(**response_kwargs),
                headers=build_402_headers(**response_kwargs),
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
