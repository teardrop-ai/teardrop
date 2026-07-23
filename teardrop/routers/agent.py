# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Agent run + tool-discovery routes (AG-UI streaming).

This router owns the two externally-facing agent endpoints:

* ``POST /agent/run`` — the AG-UI streaming endpoint (Server-Sent Events). It
  runs the pre-graph billing gate, gathers run context concurrently, drives the
  LangGraph stream, and performs post-run usage accounting, credit/x402
  settlement, and marketplace earnings recording.
* ``GET /agent/tools`` — lists the platform, org, and subscribed-marketplace
  tools available to the authenticated org.

SSE event formatting and the a2ui stream scrubber live in
``teardrop.agent_stream``. Billing primitives (credit debit, x402 settlement)
live in ``billing``; this module orchestrates them but never reimplements the
atomic-USDC accounting. The route handlers, request/response models, and run
helpers were extracted verbatim from ``teardrop.app`` and are re-exported there
for backward compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent.state import AgentState
from billing import (
    get_byok_platform_fee,
    get_current_pricing,
    get_invoice_by_run,
    get_tool_pricing_overrides,
)
from marketplace import (
    get_marketplace_catalog,
    get_subscribed_tools_catalog,
    record_marketplace_tool_usage_many,
)
from org_tools import list_org_tools
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
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.llm_config import get_org_llm_config_cached
from teardrop.memory import backfill_decision_outcome, list_run_decisions
from teardrop.rate_limit import _enforce_rate_limit
from teardrop.retention import touch_checkpoint_thread
from teardrop.tool_exclusions import add_org_tool_exclusion, list_org_tool_exclusions, remove_org_tool_exclusion
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

    async def _stream() -> AsyncIterator[dict[str, str]]:
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
        config = {
            "configurable": {
                "thread_id": scoped_thread_id,
                "_org_tools": org_lc_tools,
                "_org_tools_by_name": org_tools_by_name,
            }
        }
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
            is_byok=is_byok,
            settings=settings,
            org_llm_cfg=_org_llm_cfg,
            usage_data=usage_data,
            usage_event=usage_event,
            platform_fee=platform_fee,
            cost_usdc=cost_usdc,
            delegation_spend=delegation_spend,
            org_id=org_id,
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


# ─── /agent/tools ─────────────────────────────────────────────────────────────


class AgentToolItem(BaseModel):
    name: str
    qualified_name: str
    source: Literal["platform", "org", "marketplace"]
    access_mode: Literal["included", "subscribed"]
    display_name: str
    description: str
    cost_usdc: int
    input_schema: dict[str, Any]


@router.get("/agent/tools", tags=["Agent"], response_model=list[AgentToolItem])
async def list_agent_tools(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return all tools available to the authenticated org's agent runs."""
    org_id = _require_org_id(payload)
    settings = get_settings()

    tool_overrides = await get_tool_pricing_overrides()
    pricing = await get_current_pricing()
    default_cost = pricing.tool_call_cost if pricing else 0

    if settings.marketplace_enabled:
        platform_tools, org_tools, subscribed_tools = await asyncio.gather(
            get_marketplace_catalog(tool_overrides, default_cost, org_slug="platform"),
            list_org_tools(org_id),
            get_subscribed_tools_catalog(org_id, tool_overrides, default_cost),
        )
    else:
        org_tools = await list_org_tools(org_id)
        platform_tools = []
        subscribed_tools = []

    tools: list[AgentToolItem] = []

    for tool in platform_tools:
        tools.append(
            AgentToolItem(
                name=tool.name,
                qualified_name=tool.qualified_name,
                source="platform",
                access_mode="included",
                display_name=tool.display_name or tool.name,
                description=tool.marketplace_description or tool.description,
                cost_usdc=tool.cost_usdc,
                input_schema=tool.input_schema,
            )
        )

    for tool in org_tools:
        if not tool.is_active:
            continue
        qualified_name = f"org/{tool.name}"
        cost_usdc = tool_overrides.get(qualified_name, tool_overrides.get(tool.name, 0))
        tools.append(
            AgentToolItem(
                name=tool.name,
                qualified_name=qualified_name,
                source="org",
                access_mode="included",
                display_name=tool.name,
                description=tool.description,
                cost_usdc=cost_usdc,
                input_schema=tool.input_schema,
            )
        )

    for tool in subscribed_tools:
        tools.append(
            AgentToolItem(
                name=tool.name,
                qualified_name=tool.qualified_name,
                source="marketplace",
                access_mode="subscribed",
                display_name=tool.display_name or tool.name,
                description=tool.marketplace_description or tool.description,
                cost_usdc=tool.cost_usdc,
                input_schema=tool.input_schema,
            )
        )

    return JSONResponse(content={"tools": [t.model_dump() for t in tools]})


# ─── /agent/tool-exclusions ────────────────────────────────────────────────────
# Durable, org-scoped "hide this tool from my agent" preference. Complements the
# per-request ToolPolicy.exclude_names: persisted exclusions apply to every run
# (including scheduled/event-triggered runs) without the caller resending them.
# Advisory only — never referenced by billing/settlement.


class ToolExclusionRequest(BaseModel):
    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Internal tool name to exclude (unprefixed, e.g. 'web_search', not 'platform/web_search').",
    )


class ToolExclusionListResponse(BaseModel):
    tool_names: list[str] = Field(..., description="Persisted tool exclusions for the authenticated org.")


class ToolExclusionActionResponse(BaseModel):
    status: Literal["added"] = Field(..., description="Outcome of the exclusion write.")
    tool_name: str = Field(..., description="Normalized (unprefixed) tool name that was excluded.")


class ToolExclusionRemovedResponse(BaseModel):
    status: Literal["removed"]
    tool_name: str = Field(..., description="Normalized (unprefixed) tool name that was removed.")


@router.get("/agent/tool-exclusions", tags=["Agent"], response_model=ToolExclusionListResponse)
async def get_agent_tool_exclusions(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List the authenticated org's persisted tool exclusions."""
    org_id = _require_org_id(payload, "No org_id in token — tool exclusions require an org-scoped credential.")
    tool_names = await list_org_tool_exclusions(org_id)
    return JSONResponse(content={"tool_names": tool_names})


@router.post("/agent/tool-exclusions", tags=["Agent"], response_model=ToolExclusionActionResponse)
async def create_agent_tool_exclusion(
    body: ToolExclusionRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Persist a tool exclusion for the authenticated org."""
    org_id = _require_org_id(payload, "No org_id in token — tool exclusions require an org-scoped credential.")
    normalized = _normalize_exclusion_name(body.tool_name.strip())
    try:
        await add_org_tool_exclusion(org_id, normalized)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return JSONResponse(content={"status": "added", "tool_name": normalized})


@router.delete("/agent/tool-exclusions/{tool_name}", tags=["Agent"], response_model=ToolExclusionRemovedResponse)
async def delete_agent_tool_exclusion(
    tool_name: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Remove a persisted tool exclusion for the authenticated org."""
    org_id = _require_org_id(payload, "No org_id in token — tool exclusions require an org-scoped credential.")
    normalized = _normalize_exclusion_name(tool_name.strip())
    removed = await remove_org_tool_exclusion(org_id, normalized)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool exclusion not found.")
    return JSONResponse(content={"status": "removed", "tool_name": normalized})


# ─── /agent/decisions, /agent/runs/{run_id}/outcome ───────────────────────────
# Decision-graph read + outcome-labeling surface. Read-only telemetry: these
# routes never gate billing/settlement and never mutate usage_events.


class RunOutcomeRequest(BaseModel):
    rating: int = Field(..., ge=-1, le=1, description="-1 (bad outcome), 0 (neutral), or 1 (good outcome)")


class AgentDecisionRecord(BaseModel):
    id: str = Field(..., description="Decision record ID (UUID string).")
    run_id: str = Field(..., description="Run this decision summarizes.")
    task_class: str = Field(default="", description="Auto-classified task type; empty string if unclassified.")
    action: str = Field(default="", description="Action the planner took.")
    reasoning: str = Field(default="", description="Planner's stated reasoning for the action.")
    confidence: float | None = Field(default=None, description="Planner confidence score, if recorded.")
    tool_names: list[str] = Field(default_factory=list, description="Tools used while making this decision.")
    outcome: int = Field(..., ge=-1, le=1, description="-1 (bad), 0 (neutral/unlabeled), or 1 (good).")
    outcome_source: str = Field(default="", description="Origin of the outcome label (e.g. 'feedback'); empty if unlabeled.")
    created_at: str = Field(..., description="ISO 8601 creation timestamp.")


class AgentDecisionListResponse(BaseModel):
    items: list[AgentDecisionRecord]
    next_cursor: str | None = Field(
        default=None, description="ISO datetime cursor for the next page; null when no more items remain."
    )


@router.get("/agent/decisions", tags=["Agent"], response_model=AgentDecisionListResponse)
async def list_agent_decisions(
    payload: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="ISO datetime cursor for pagination"),
) -> JSONResponse:
    """List stored decision records for the authenticated org (newest first, cursor-paginated).

    Each record summarizes one agent run: the action taken, reasoning, task
    classification, tools used, and — once labeled — an outcome rating. This
    is the decision graph read surface; it is populated asynchronously after
    ``POST /agent/run`` completes and may lag briefly behind the SSE stream.
    """
    org_id = _require_org_id(payload, "No org_id in token — decisions require an org-scoped credential.")

    from shared.pagination import parse_cursor  # noqa: PLC0415

    cursor_dt = parse_cursor(cursor)
    rows = await list_run_decisions(org_id, limit, cursor_dt)
    serialized = [
        {
            "id": r["id"],
            "run_id": r["run_id"],
            "task_class": r["task_class"],
            "action": r["action"],
            "reasoning": r["reasoning"],
            "confidence": r["confidence"],
            "tool_names": r["tool_names"],
            "outcome": r["outcome"],
            "outcome_source": r["outcome_source"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
    next_cursor = serialized[-1]["created_at"] if serialized else None
    return JSONResponse(content={"items": serialized, "next_cursor": next_cursor})


class RunOutcomeResponse(BaseModel):
    status: Literal["recorded"]


@router.patch("/agent/runs/{run_id}/outcome", tags=["Agent"], response_model=RunOutcomeResponse)
async def set_agent_run_outcome(
    run_id: str,
    body: RunOutcomeRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Label the ground-truth outcome (-1/0/1) of a past run — feeds the decision graph.

    Ownership is verified the same way as marketplace tool feedback
    (``submit_marketplace_tool_feedback``): the run must belong to the
    authenticated user's own invoice history. The label is applied once —
    resubmitting after a label already exists returns 404 rather than
    silently overwriting it.
    """
    org_id = _require_org_id(payload, "No org_id in token — outcomes require an org-scoped credential.")
    user_id = payload.get("sub", "")

    invoice = await get_invoice_by_run(run_id, user_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found for this account.")

    updated = await backfill_decision_outcome(run_id, org_id, body.rating, source="explicit")
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No decision record found for this run, or its outcome was already set.",
        )
    return JSONResponse(content={"status": "recorded"})
