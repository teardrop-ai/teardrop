# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""AG-UI streaming agent run route."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from agent.runtime_context import AgentRunContext, agent_run_context
from agent.state import AgentState
from billing import get_byok_platform_fee
from marketplace import record_marketplace_tool_usage_many
from teardrop.agent_event_loop import stream_graph_events
from teardrop.agent_post_run import (
    calculate_run_cost,
    dispatch_settlement,
    fetch_usage_snapshot,
    record_post_run_telemetry,
)
from teardrop.agent_runtime import (
    _prepare_run_context,
    _record_marketplace_earnings,
    _run_billing_gate,
)
from teardrop.agent_schemas import (
    AgentRunRequest,
    ToolPolicy,  # noqa: F401  — re-exported via teardrop.app for backward compat
    _normalize_exclusion_name,
)
from teardrop.agent_stream import (
    _EV_DONE,
    _EV_RUN_FINISHED,
    _EV_RUN_STARTED,
    _EV_USAGE_SUMMARY,
    _sse_event,
)
from teardrop.agent_telemetry import _log_agent_memory
from teardrop.concurrency import try_acquire_agent_run_slot
from teardrop.config import get_settings
from teardrop.dependencies import require_auth
from teardrop.llm_config import get_org_llm_config_cached
from teardrop.rate_limit import _enforce_rate_limit
from teardrop.retention import touch_checkpoint_thread
from teardrop.routers.agent_decisions import (
    AgentDecisionListResponse,  # noqa: F401
    AgentDecisionRecord,  # noqa: F401
    RunOutcomeRequest,  # noqa: F401
    RunOutcomeResponse,  # noqa: F401
    list_agent_decisions,  # noqa: F401
    set_agent_run_outcome,  # noqa: F401
)
from teardrop.routers.agent_tools import (
    AgentToolItem,  # noqa: F401
    ToolExclusionActionResponse,  # noqa: F401
    ToolExclusionListResponse,  # noqa: F401
    ToolExclusionRemovedResponse,  # noqa: F401
    ToolExclusionRequest,  # noqa: F401
    create_agent_tool_exclusion,  # noqa: F401
    delete_agent_tool_exclusion,  # noqa: F401
    get_agent_tool_exclusions,  # noqa: F401
    list_agent_tools,  # noqa: F401
)
from teardrop.usage import UsageEvent, record_telemetry_run_started, record_usage_event

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


@router.post("/agent/run", tags=["Agent"])
async def agent_run(
    body: AgentRunRequest,
    request: Request,
    payload: dict = Depends(require_auth),
) -> EventSourceResponse:
    """AG-UI streaming endpoint.

    Accepts a user message and streams AG-UI-compatible Server-Sent Events
    until the agent completes or errors.  Supports multi-turn via thread_id.
    Thread state is scoped to the authenticated user.
    """
    user_id: str = payload["sub"]
    await _enforce_rate_limit(
        f"run:{user_id}",
        settings.rate_limit_agent_rpm,
        detail="Rate limit exceeded. Please slow down.",
    )

    org_id: str = payload.get("org_id", "")

    # ── Per-org aggregate rate limit ────────────────────────────────────────
    # Guards against a single org saturating the LLM pool across many users.
    org_rpm: int = settings.rate_limit_org_agent_rpm
    if org_id and isinstance(org_rpm, int):
        await _enforce_rate_limit(
            f"run:org:{org_id}",
            org_rpm,
            detail="Organization rate limit exceeded. Please slow down.",
            extra_headers={"X-RateLimit-Scope": "org"},
        )

    run_id = str(uuid.uuid4())
    scoped_thread_id = f"{user_id}:{body.thread_id}"
    logger.info(
        "agent_run start run_id=%s thread_id=%s user=%s",
        run_id,
        scoped_thread_id,
        user_id,
    )

    # ── Billing gate ────────────────────────────────────────────────────────
    # Resolve BYOK status early — used by both the gate and the downstream
    # debit step in _stream().
    _org_llm_cfg = await get_org_llm_config_cached(org_id)
    is_byok = _org_llm_cfg.is_byok if _org_llm_cfg else False
    # For the pre-run billing gate we always use the floor (actual usage is unknown).
    # Token-based cost is computed post-run at the debit step.
    platform_fee = get_byok_platform_fee(is_byok)

    billing, gate_response = await _run_billing_gate(request, payload, org_id, is_byok=is_byok, platform_fee=platform_fee)
    if gate_response is not None:
        return gate_response

    run_lease = try_acquire_agent_run_slot()
    if run_lease is None:
        payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
        if payment_header and getattr(billing, "billing_method", "") == "x402":
            from billing import release_payment_nonce

            await release_payment_nonce(payment_header)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent run capacity is temporarily exhausted. Please retry shortly.",
            headers={"Retry-After": "1"},
        )

    async def _stream() -> AsyncIterator[dict[str, str]]:
        try:
            async for event in _run_stream():
                yield event
        finally:
            run_lease.release()

    async def _run_stream() -> AsyncIterator[dict[str, str]]:
        start_time = time.monotonic()
        yield _sse_event(_EV_RUN_STARTED, {"run_id": run_id, "thread_id": body.thread_id})
        await record_telemetry_run_started(run_id, org_id, "api")
        _log_agent_memory("stream_start", run_id=run_id)

        # ── Pre-graph init: gather all independent calls concurrently ─────
        mem_settings = get_settings()
        prepare_started = time.monotonic()
        _log_agent_memory("prepare_run_context_start", run_id=run_id)
        ctx = await _prepare_run_context(
            org_id=org_id,
            user_message=body.message,
            billing=billing,
            mem_settings=mem_settings,
        )
        _log_agent_memory(
            "prepare_run_context_end",
            run_id=run_id,
            elapsed_ms=int((time.monotonic() - prepare_started) * 1000),
        )
        graph = ctx.graph
        org_lc_tools = ctx.org_lc_tools
        org_tools_by_name = ctx.org_tools_by_name
        mp_by_name = ctx.mp_by_name
        recalled = ctx.recalled
        llm_config = ctx.llm_config
        _org_name = ctx.org_name
        _credit_balance_usdc = ctx.credit_balance_usdc
        excluded_tools: frozenset[str] = frozenset()
        if body.tool_policy and body.tool_policy.exclude_names:
            excluded_tools = frozenset(_normalize_exclusion_name(name) for name in body.tool_policy.exclude_names)
        excluded_tools |= frozenset(ctx.persisted_excluded_tools)
        if getattr(ctx, "is_promotional_credit", False):
            excluded_tools |= frozenset(ctx.mp_by_name)

        initial_state = AgentState(
            messages=[HumanMessage(content=body.message)],
            metadata={
                **body.context,
                "thread_id": scoped_thread_id,
                "run_id": run_id,
                "user_id": user_id,
                "org_id": org_id,
                "_usage": {
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
                },
                "_excluded_tool_names": list(excluded_tools),
                "_memories": recalled,
                "_llm_config": llm_config,
                "_org_name": _org_name,
                "_user_role": payload.get("role", "user"),
                "_user_wallet_address": payload.get("address") or None,
                "_credit_balance_usdc": _credit_balance_usdc,
                "_jwt_token": (request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None),
                "emit_ui": body.emit_ui,
            },
        )
        config = {"configurable": {"thread_id": scoped_thread_id}}
        await touch_checkpoint_thread(scoped_thread_id)

        # ── Implicit correction detection (fire-and-forget) ──────────────
        # Check if this run is a correction of the immediately prior turn
        # on the same thread. Runs concurrently with the graph loop.
        from teardrop.memory import detect_implicit_correction  # noqa: PLC0415

        asyncio.create_task(detect_implicit_correction(org_id, scoped_thread_id, body.message))

        # Drive the LangGraph event-dispatch loop. The generator yields the
        # per-token / per-tool / per-surface SSE frames and signals early
        # termination (cancellation or unhandled error) via the result dict so
        # post-run usage accounting is skipped exactly as before.
        _loop_result: dict[str, Any] = {}
        with agent_run_context(AgentRunContext(org_lc_tools, org_tools_by_name)):
            async for _sse in stream_graph_events(
                graph=graph,
                initial_state=initial_state,
                config=config,
                run_id=run_id,
                settings=settings,
                org_id=org_id,
                payload=payload,
                result=_loop_result,
            ):
                yield _sse
        if _loop_result.get("terminated"):
            if _loop_result.get("termination_reason") == "failed":
                state_snapshot, usage_data = await fetch_usage_snapshot(
                    graph=graph,
                    config=config,
                    run_id=run_id,
                    settings=settings,
                )
                record_post_run_telemetry(
                    run_id=run_id,
                    org_id=org_id,
                    user_id=user_id,
                    usage_data=usage_data,
                    state_values=(state_snapshot.values or {}) if state_snapshot is not None else None,
                    settings=mem_settings,
                    outcome=-1,
                    outcome_source="auto",
                    thread_id=scoped_thread_id,
                    user_message=body.message,
                )
            return

        # ── Usage accounting (log-only, never blocks) ─────────────────────
        duration_ms = int((time.monotonic() - start_time) * 1000)
        # state_snapshot is also read later by the memory-extraction kickoff.
        state_snapshot, usage_data = await fetch_usage_snapshot(
            graph=graph,
            config=config,
            run_id=run_id,
            settings=settings,
        )

        cost_usdc = await calculate_run_cost(
            usage_data=usage_data,
            llm_config=llm_config,
            settings=settings,
            is_byok=is_byok,
            org_llm_cfg=_org_llm_cfg,
            platform_fee=platform_fee,
        )

        logger.info(
            "agent_run diagnostic_summary run_id=%s org_id=%s duration_ms=%d "
            "tokens_in=%d tokens_out=%d tool_calls=%d cost_usdc_atomic=%d cost_usd=$%.6f",
            run_id,
            org_id,
            duration_ms,
            usage_data.get("tokens_in", 0),
            usage_data.get("tokens_out", 0),
            usage_data.get("tool_calls", 0),
            cost_usdc,
            cost_usdc / 1_000_000,
        )

        usage_event = UsageEvent(
            user_id=user_id,
            org_id=org_id,
            thread_id=scoped_thread_id,
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
            provider=llm_config["provider"] if llm_config else settings.agent_provider,
            model=llm_config["model"] if llm_config else settings.agent_model,
            llm_turns=usage_data.get("turns") or [],
            source="api",
        )
        await record_usage_event(usage_event)

        # ── ML telemetry (non-financial, fire-and-forget) ────────────────
        state_values = (state_snapshot.values or {}) if state_snapshot is not None else None
        task_status = state_values.get("task_status", "") if state_values else ""
        task_status = getattr(task_status, "value", None) or str(task_status)
        record_post_run_telemetry(
            run_id=run_id,
            org_id=org_id,
            user_id=user_id,
            usage_data=usage_data,
            state_values=state_values,
            settings=mem_settings,
            outcome=-1 if task_status.strip().lower().endswith("failed") else 1,
            outcome_source="auto",
            thread_id=scoped_thread_id,
            user_message=body.message,
        )

        # ── Settlement / credit debit (after usage recorded) ─────────────
        delegation_spend = usage_data.get("delegation_spend_usdc", 0)

        _settlement_result: dict[str, Any] = {}
        async for _sse in dispatch_settlement(
            billing=billing,
            settings=settings,
            usage_event=usage_event,
            platform_fee=platform_fee,
            cost_usdc=cost_usdc,
            delegation_spend=delegation_spend,
            org_id=org_id,
            principal_id=user_id,
            run_id=run_id,
            result=_settlement_result,
        ):
            yield _sse
        marketplace_stats_billable = _settlement_result.get("marketplace_stats_billable", False) and not getattr(
            ctx, "is_promotional_credit", False
        )

        # ── Record marketplace tool earnings + usage stats ───────────────
        # Both are gated on ``marketplace_stats_billable`` (True only after a
        # confirmed credit debit or confirmed x402 settlement). Recording
        # earnings before settlement succeeds would credit tool authors for
        # runs the caller never actually paid for.
        if marketplace_stats_billable:
            await _record_marketplace_earnings(
                mp_by_name=mp_by_name,
                tool_names_used=usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
                caller_org_id=org_id,
            )
            billable_tool_names = usage_data.get("billable_tool_names", usage_data.get("tool_names", []))
            if isinstance(billable_tool_names, list):
                asyncio.create_task(record_marketplace_tool_usage_many([str(name) for name in billable_tool_names]))

        yield _sse_event(
            _EV_USAGE_SUMMARY,
            {
                "run_id": run_id,
                "tokens_in": usage_event.tokens_in,
                "tokens_out": usage_event.tokens_out,
                "cache_read_tokens": usage_event.cache_read_tokens,
                "cache_creation_tokens": usage_event.cache_creation_tokens,
                "tool_calls": usage_event.tool_calls,
                "duration_ms": usage_event.duration_ms,
                "cost_usdc": usage_event.cost_usdc,
                "platform_fee_usdc": platform_fee,
                "delegation_cost_usdc": delegation_spend,
            },
        )
        _log_agent_memory(
            "stream_end",
            run_id=run_id,
            elapsed_ms=int((time.monotonic() - start_time) * 1000),
        )
        yield _sse_event(_EV_RUN_FINISHED, {"run_id": run_id})
        yield _sse_event(_EV_DONE, {"run_id": run_id})

    return EventSourceResponse(_stream())
