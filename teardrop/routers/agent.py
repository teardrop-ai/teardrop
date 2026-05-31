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
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from agent.state import AgentState
from billing import (
    calculate_byok_orchestration_cost,
    calculate_run_cost_usdc,
    debit_credit,
    enqueue_failed_settlement,
    get_byok_platform_fee,
    get_current_pricing,
    get_tool_pricing_overrides,
    record_settlement,
    settle_payment,
    verify_settlement_on_chain,
)
from marketplace import (
    get_marketplace_catalog,
    get_subscribed_tools_catalog,
    record_marketplace_tool_usage_many,
)
from org_tools import list_org_tools
from teardrop.agent_runtime import (
    _prepare_run_context,
    _record_marketplace_earnings,
    _run_billing_gate,
)
from teardrop.agent_stream import (
    _EV_BILLING_SETTLEMENT,
    _EV_CUSTOM,
    _EV_DONE,
    _EV_ERROR,
    _EV_RUN_FINISHED,
    _EV_RUN_STARTED,
    _EV_SURFACE_UPDATE,
    _EV_TEXT_MSG_CONTENT,
    _EV_TEXT_MSG_END,
    _EV_TEXT_MSG_START,
    _EV_TOOL_CALL_END,
    _EV_TOOL_CALL_START,
    _EV_USAGE_SUMMARY,
    _A2UIStreamFilter,
    _recover_planner_suffix,
    _should_flush_planner_buffer,
    _sse_event,
)
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.llm_config import get_org_llm_config_cached
from teardrop.memory import extract_and_store_memories
from teardrop.rate_limit import _enforce_rate_limit
from teardrop.usage import UsageEvent, record_usage_event

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


# ─── Memory telemetry ─────────────────────────────────────────────────────────


def _process_rss_bytes() -> int | None:
    """Return current process RSS in bytes, or None when unavailable."""
    proc_status = Path("/proc/self/status")
    if proc_status.exists():
        try:
            for line in proc_status.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    # Format: VmRSS:\t  123456 kB
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
        except Exception:
            logger.debug("Failed reading /proc/self/status for RSS telemetry", exc_info=True)

    try:
        import resource  # noqa: PLC0415

        rss_raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if rss_raw <= 0:
            return None
        if sys.platform == "darwin":
            return rss_raw
        return rss_raw * 1024
    except Exception:
        return None


def _log_agent_memory(stage: str, *, run_id: str, elapsed_ms: int | None = None) -> None:
    """Emit lightweight RSS telemetry for memory-spike diagnosis."""
    if not settings.agent_memory_telemetry_enabled:
        return
    rss_bytes = _process_rss_bytes()
    if rss_bytes is None:
        return
    suffix = f" elapsed_ms={elapsed_ms}" if elapsed_ms is not None else ""
    logger.info(
        "agent_run memory stage=%s run_id=%s rss_mib=%.1f%s",
        stage,
        run_id,
        rss_bytes / (1024 * 1024),
        suffix,
    )


# ─── Request / response models ────────────────────────────────────────────────


_TOOL_NAMESPACE_PREFIXES = ("platform/", "org/")


def _normalize_exclusion_name(name: str) -> str:
    """Map API-facing qualified names to internal executor/binder tool keys."""
    for prefix in _TOOL_NAMESPACE_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


class ToolPolicy(BaseModel):
    exclude_names: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Qualified tool names to exclude for this run.",
    )

    @field_validator("exclude_names")
    @classmethod
    def _validate_exclude_names(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            stripped = value.strip()
            if not stripped:
                raise ValueError("exclude_names entries must be non-empty strings")
            if len(stripped) > 200:
                raise ValueError("exclude_names entries must be 200 characters or fewer")
            normalized.append(stripped)
        return normalized


class AgentRunRequest(BaseModel):
    message: str = Field(..., description="User message to send to the agent", max_length=4096)
    thread_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Conversation thread ID for multi-turn sessions",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional extra context passed to the agent state metadata",
    )
    emit_ui: bool = Field(
        default=True,
        description="Whether to generate structured UI components in the final output.",
    )
    tool_policy: ToolPolicy | None = Field(
        default=None,
        description="Optional per-run tool exclusion policy.",
    )


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
                "_org_tools": org_lc_tools,
                "_org_tools_by_name": org_tools_by_name,
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

        # Streaming a2ui-block scrubber for planner text tokens. ui_generator
        # also calls an LLM (raw JSON output); we additionally guard by
        # langgraph_node so its tokens never reach TEXT_MESSAGE_CONTENT.
        _text_filter = _A2UIStreamFilter()
        _last_msg_id: str = run_id
        _planner_token_buffer: list[tuple[str, str]] = []
        _text_emitted = False

        def _coerce_stream_text(content: Any) -> str:
            """Normalise provider-specific content blocks to plain text."""
            if isinstance(content, list):
                return "".join(
                    block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                    for block in content
                    if (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) == "text"
                )
            return content if isinstance(content, str) else str(content or "")

        try:
            async for event in graph.astream_events(
                initial_state.model_dump(),
                config=config,
                version="v2",
            ):
                event_name: str = event.get("event", "")
                event_data: dict[str, Any] = event.get("data", {})
                node_name: str = event.get("name", "")

                # --- Text streaming from the planner (LLM tokens) ---
                if event_name == "on_chat_model_stream":
                    # Only forward tokens originating from the planner node.
                    # The ui_generator node also invokes an LLM whose raw JSON
                    # output must not surface as TEXT_MESSAGE_CONTENT.
                    if event.get("metadata", {}).get("langgraph_node") != "planner":
                        await asyncio.sleep(0)
                        continue
                    chunk = event_data.get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        msg_id = event.get("run_id", run_id)
                        # Anthropic returns chunk.content as a list of content
                        # blocks ([{"type": "text", "text": "...", "index": 0}]).
                        # Normalise to a plain string so the SDK delta contract
                        # (string) is always satisfied regardless of provider.
                        raw_content = chunk.content
                        if isinstance(raw_content, list):
                            delta = "".join(
                                block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                                for block in raw_content
                                if (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) == "text"
                            )
                        else:
                            delta = raw_content
                        if delta:
                            _last_msg_id = msg_id
                            clean = _text_filter.feed(delta)
                            if clean:
                                _planner_token_buffer.append((msg_id, clean))

                # --- Tool call start ---
                elif event_name == "on_tool_start":
                    yield _sse_event(
                        _EV_TOOL_CALL_START,
                        {
                            "tool_call_id": event.get("run_id", ""),
                            "tool_name": node_name,
                            "args": event_data.get("input", {}),
                        },
                    )

                # --- Tool call end ---
                elif event_name == "on_tool_end":
                    tool_call_id = event.get("run_id", "")
                    raw_output = event_data.get("output", "")
                    yield _sse_event(
                        _EV_TOOL_CALL_END,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": node_name,
                            "output": str(raw_output),
                        },
                    )
                    # Emit structured tool output as a Custom event (ag-ui spec).
                    # TEXT_MESSAGE_CONTENT carries human-readable text only;
                    # TOOL_OUTPUT carries machine-readable structured data.
                    # Consumers that don't recognise this event ignore it cleanly.
                    structured: Any = raw_output
                    if isinstance(raw_output, str):
                        try:
                            structured = json.loads(raw_output)
                        except (json.JSONDecodeError, ValueError):
                            pass  # Leave as plain string — valid for text-only tools
                    elif hasattr(raw_output, "model_dump"):
                        structured = raw_output.model_dump()
                    yield _sse_event(
                        _EV_CUSTOM,
                        {
                            "name": "TOOL_OUTPUT",
                            "value": {
                                "tool_call_id": tool_call_id,
                                "tool_name": node_name,
                                "data": structured,
                            },
                        },
                    )

                # --- Planner finished: conditionally flush buffered text ---
                elif event_name == "on_chain_end" and node_name == "planner":
                    output = event_data.get("output", {})
                    # task_status may be a TaskStatus(str, Enum) instance.
                    # In Python 3.12, str(TaskStatus.GENERATING_UI) returns
                    # "TaskStatus.GENERATING_UI", not the value "generating_ui",
                    # so prefer the enum's `.value` when present.
                    _ts = output.get("task_status", "")
                    status_ = (getattr(_ts, "value", None) or str(_ts)).strip().lower()

                    if _should_flush_planner_buffer(status_):
                        emitted_chunks: list[tuple[str, str]] = []
                        for message_id, delta in _planner_token_buffer:
                            emitted_chunks.append((message_id, delta))
                        _planner_token_buffer.clear()

                        remainder = _text_filter.flush()
                        if remainder:
                            emitted_chunks.append((_last_msg_id, remainder))

                        # If streamed deltas were fully suppressed (e.g. response began
                        # with an a2ui fence), derive visible prose from planner output.
                        if not emitted_chunks:
                            planner_messages = output.get("messages", [])
                            if planner_messages:
                                last_planner_msg = planner_messages[-1]
                                planner_content = (
                                    getattr(last_planner_msg, "content", "")
                                    if not isinstance(last_planner_msg, dict)
                                    else last_planner_msg.get("content", "")
                                )
                                normalized = _coerce_stream_text(planner_content)
                                if normalized:
                                    fallback_filter = _A2UIStreamFilter()
                                    fallback_text = fallback_filter.feed(normalized) + fallback_filter.flush()
                                    if fallback_text.strip():
                                        emitted_chunks.append((_last_msg_id, fallback_text))

                        if emitted_chunks:
                            planner_messages = output.get("messages", [])
                            if planner_messages:
                                last_planner_msg = planner_messages[-1]
                                planner_content = (
                                    getattr(last_planner_msg, "content", "")
                                    if not isinstance(last_planner_msg, dict)
                                    else last_planner_msg.get("content", "")
                                )
                                normalized = _coerce_stream_text(planner_content)
                                suffix = _recover_planner_suffix(emitted_chunks, normalized)
                                if suffix:
                                    emitted_chunks.append((_last_msg_id, suffix))

                        if emitted_chunks:
                            yield _sse_event(_EV_TEXT_MSG_START, {"message_id": _last_msg_id})
                            for message_id, delta in emitted_chunks:
                                _text_emitted = True
                                yield _sse_event(
                                    _EV_TEXT_MSG_CONTENT,
                                    {"message_id": message_id, "delta": delta},
                                )
                            yield _sse_event(_EV_TEXT_MSG_END, {"message_id": _last_msg_id})
                    else:
                        # Intermediate/planner-failed turns are not user-facing.
                        _planner_token_buffer.clear()
                        _text_filter.flush()

                    # P1: Explicitly signal timeouts or rate-limits to the client.
                    # This prevents the client from assuming the run finished normally
                    # when the LLM timed out or failed.
                    _err_type = output.get("error_type")
                    if _err_type in ("timeout", "rate_limit"):
                        _msg = "The model is currently unresponsive. Please try your request again in a moment."
                        if _err_type == "timeout":
                            _msg = (
                                f"The model timed out after {settings.agent_llm_timeout_seconds}s. "
                                "Your request may be too complex, or the provider is overloaded."
                            )

                        yield _sse_event(
                            _EV_CUSTOM,
                            {
                                "name": "AGENT_WARNING",
                                "value": {
                                    "type": _err_type,
                                    "message": _msg,
                                },
                            },
                        )
                    elif output.get("error"):
                        # Generic planner failures should be surfaced without
                        # exposing provider/internal error details to clients.
                        yield _sse_event(
                            _EV_CUSTOM,
                            {
                                "name": "AGENT_WARNING",
                                "value": {
                                    "type": "error",
                                    "message": "The request could not be completed. Please try again.",
                                },
                            },
                        )

                # --- Node outputs (state snapshots) ---
                elif event_name == "on_chain_end" and node_name == "tool_executor":
                    output = event_data.get("output", {})
                    # P1: Explicitly signal timeouts or rate-limits to the client.
                    # This prevents the client from assuming the run finished normally
                    # when the LLM timed out or failed.
                    _err_type = output.get("error_type")
                    if _err_type in ("timeout", "rate_limit"):
                        _msg = "The model is currently unresponsive. Please try your request again in a moment."
                        if _err_type == "timeout":
                            _msg = (
                                f"The model timed out after {settings.agent_llm_timeout_seconds}s. "
                                "Your request may be too complex, or the provider is overloaded."
                            )

                        yield _sse_event(
                            _EV_CUSTOM,
                            {
                                "name": "AGENT_WARNING",
                                "value": {
                                    "type": _err_type,
                                    "message": _msg,
                                },
                            },
                        )

                # --- Node outputs (state snapshots) ---
                elif event_name == "on_chain_end" and node_name == "ui_generator":
                    output = event_data.get("output", {})
                    ui_components = output.get("ui_components", [])
                    if ui_components:
                        yield _sse_event(
                            _EV_SURFACE_UPDATE,
                            {
                                "surface_id": run_id,
                                "components": [c if isinstance(c, dict) else c.model_dump() for c in ui_components],
                            },
                        )
                    if not _text_emitted:
                        yield _sse_event(
                            _EV_CUSTOM,
                            {
                                "name": "AGENT_WARNING",
                                "value": {
                                    "type": "empty_response",
                                    "message": (
                                        "The run completed without visible text output. Please retry or simplify your request."
                                    ),
                                },
                            },
                        )

                # --- Yield control to allow concurrent requests ---
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            logger.info("agent_run cancelled run_id=%s", run_id)
            yield _sse_event(_EV_ERROR, {"run_id": run_id, "error": "Request cancelled."})
            return

        except Exception as exc:
            logger.error("agent_run error run_id=%s: %s", run_id, exc, exc_info=True)
            try:
                import sentry_sdk

                with sentry_sdk.new_scope() as scope:
                    scope.set_tag("org_id", str(org_id))
                    scope.set_tag("run_id", str(run_id))
                    scope.set_tag("auth_method", str(payload.get("auth_method", "")))
                    sentry_sdk.capture_exception(exc)
            except Exception:  # pragma: no cover - sentry is best-effort
                pass
            # Do not leak internal exception details to clients in production.
            error_msg = (
                f"Agent error: {exc}"
                if settings.app_env != "production"
                else "An internal error occurred. Check server logs for details."
            )
            yield _sse_event(
                _EV_ERROR,
                {"run_id": run_id, "error": error_msg},
            )
            return

        # ── Usage accounting (log-only, never blocks) ─────────────────────
        duration_ms = int((time.monotonic() - start_time) * 1000)
        usage_data: dict[str, Any] = {}
        # state_snapshot is also read later by the memory-extraction kickoff;
        # initialise to None so a timeout/exception leaves it well-defined.
        state_snapshot = None
        state_started = time.monotonic()
        _log_agent_memory("aget_state_start", run_id=run_id)
        try:
            state_snapshot = await asyncio.wait_for(
                graph.aget_state(config),
                timeout=settings.agent_state_snapshot_timeout_seconds,
            )
            usage_data = (state_snapshot.values or {}).get("metadata", {}).get("_usage", {})
        except asyncio.TimeoutError:
            logger.warning(
                "agent_run aget_state timed out after %.1fs run_id=%s; skipping usage data",
                settings.agent_state_snapshot_timeout_seconds,
                run_id,
            )
        except Exception:
            logger.debug("Could not retrieve final state for usage", exc_info=True)
        finally:
            _log_agent_memory(
                "aget_state_end",
                run_id=run_id,
                elapsed_ms=int((time.monotonic() - state_started) * 1000),
            )

        # Calculate usage-based cost from live pricing rule (never blocks the stream).
        cost_usdc = 0
        try:
            _run_provider = llm_config["provider"] if llm_config else settings.agent_provider
            _run_model = llm_config["model"] if llm_config else settings.agent_model
            turns = usage_data.get("turns") if isinstance(usage_data, dict) else None
            if isinstance(turns, list) and turns:
                token_cost_total = 0
                for turn in turns:
                    if not isinstance(turn, dict):
                        continue
                    turn_provider = str(turn.get("provider") or _run_provider)
                    turn_model = str(turn.get("model") or _run_model)
                    turn_usage = {
                        "tokens_in": int(turn.get("tokens_in", 0)),
                        "tokens_out": int(turn.get("tokens_out", 0)),
                        # Token-only per turn; tools are charged separately once per run.
                        "billable_tool_calls": 0,
                        "billable_tool_names": [],
                    }
                    token_cost_total += await calculate_run_cost_usdc(turn_usage, turn_provider, turn_model)

                tool_usage = {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "billable_tool_calls": int(usage_data.get("billable_tool_calls", usage_data.get("tool_calls", 0))),
                    "billable_tool_names": usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
                }
                tool_cost_total = await calculate_run_cost_usdc(tool_usage, _run_provider, _run_model)
                cost_usdc = token_cost_total + tool_cost_total
            else:
                cost_usdc = await calculate_run_cost_usdc(usage_data, _run_provider, _run_model)
        except Exception:
            logger.debug("Could not calculate run cost", exc_info=True)

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
        )
        await record_usage_event(usage_event)

        # ── Extract and store memories (fire-and-forget) ─────────────────
        if mem_settings.memory_enabled and state_snapshot is not None:
            try:
                state_msgs = ((state_snapshot.values or {}).get("messages", []))[-10:]
                if state_msgs:
                    billable_tool_names = usage_data.get("billable_tool_names", usage_data.get("tool_names", []))
                    if not isinstance(billable_tool_names, list):
                        billable_tool_names = []
                    asyncio.create_task(
                        extract_and_store_memories(
                            org_id,
                            user_id,
                            state_msgs,
                            run_id,
                            tool_names_used=[str(name) for name in billable_tool_names],
                        )
                    )
            except Exception:
                logger.debug("Memory extraction kickoff failed", exc_info=True)

        # ── Settlement / credit debit (after usage recorded) ─────────────
        delegation_spend = usage_data.get("delegation_spend_usdc", 0)
        marketplace_stats_billable = False

        if billing.verified:
            # Determine what to charge BYOK orgs.
            # - byok_tier_pricing_enabled=True (migration 041 applied): per-token
            #   orchestration cost floored at byok_platform_fee_usdc.
            # - byok_tier_pricing_enabled=False (legacy / pre-migration): flat fee.
            # Non-BYOK orgs always pay the full LLM cost.
            if is_byok and settings.byok_tier_pricing_enabled:
                _run_provider = (_org_llm_cfg.provider if _org_llm_cfg else "") or ""
                _run_model = (_org_llm_cfg.model if _org_llm_cfg else "") or ""
                debit_amount = await calculate_byok_orchestration_cost(
                    usage_data.get("tokens_in", 0),
                    usage_data.get("tokens_out", 0),
                    provider=_run_provider,
                    model=_run_model,
                )
            else:
                # Legacy: flat fee for BYOK, full model cost for non-BYOK.
                debit_amount = platform_fee if is_byok else cost_usdc

            if billing.billing_method == "credit":
                # Debit actual run cost (or platform fee for BYOK) from org's prepaid balance.
                success, deducted_amount = await debit_credit(org_id, debit_amount, reason=f"run:{run_id}")
                if success:
                    marketplace_stats_billable = True
                    await record_settlement(usage_event.id, deducted_amount, "", "settled")
                    yield _sse_event(
                        _EV_BILLING_SETTLEMENT,
                        {
                            "run_id": run_id,
                            "amount_usdc": deducted_amount,
                            "tx_hash": "",
                            "network": "credit",
                            "delegation_cost_usdc": delegation_spend,
                            "platform_fee_usdc": platform_fee,
                        },
                    )
                else:
                    await record_settlement(usage_event.id, debit_amount, "", "failed")
                    await enqueue_failed_settlement(
                        usage_event.id,
                        org_id,
                        run_id,
                        "credit",
                        debit_amount,
                    )
                    logger.warning("Credit debit failed run_id=%s org_id=%s", run_id, org_id)
            else:
                # x402 on-chain settlement.
                # Clamp to the upto ceiling the client signed; otherwise settlement
                # would fail because the signed amount cannot cover the higher cost.
                if settings.x402_scheme == "upto":
                    upto_ceiling = settings.x402_upto_max_amount_atomic
                    if upto_ceiling > 0 and cost_usdc > upto_ceiling:
                        logger.warning(
                            "Run cost exceeds x402 upto ceiling; clamping run_id=%s org_id=%s cost_usdc=%d ceiling_usdc=%d",
                            run_id,
                            org_id,
                            cost_usdc,
                            upto_ceiling,
                        )
                        cost_usdc = upto_ceiling
                # Hard timeout on the facilitator HTTP call so a slow/unreachable
                # facilitator can never hold the SSE stream open indefinitely.
                # On timeout we route to the same failure path used when the
                # facilitator returns a non-success response: enqueue for retry
                # by the background worker (see process_pending_settlements).
                settlement_timed_out = False
                try:
                    billing_settled = await asyncio.wait_for(
                        settle_payment(billing, actual_cost_usdc=cost_usdc),
                        timeout=settings.x402_settlement_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    settlement_timed_out = True
                    billing_settled = billing  # placeholder; settled=False by default
                    logger.warning(
                        "Settlement timed out after %ds run_id=%s org_id=%s; enqueued for retry",
                        settings.x402_settlement_timeout_seconds,
                        run_id,
                        org_id,
                    )
                if not settlement_timed_out and billing_settled.settled:
                    marketplace_stats_billable = True
                    await record_settlement(
                        usage_event.id,
                        billing_settled.amount_usdc,
                        billing_settled.tx_hash,
                        "settled",
                    )
                    yield _sse_event(
                        _EV_BILLING_SETTLEMENT,
                        {
                            "run_id": run_id,
                            "amount_usdc": billing_settled.amount_usdc,
                            "tx_hash": billing_settled.tx_hash,
                            "network": settings.x402_network,
                            "delegation_cost_usdc": delegation_spend,
                            "platform_fee_usdc": platform_fee,
                        },
                    )
                    if billing_settled.tx_hash:
                        try:
                            chain_id = int(str(settings.x402_network).rsplit(":", 1)[-1])
                        except (TypeError, ValueError):
                            logger.warning(
                                "Skipping x402 receipt check due to unparseable network: %s",
                                settings.x402_network,
                            )
                        else:
                            asyncio.create_task(
                                verify_settlement_on_chain(
                                    usage_event.id,
                                    billing_settled.tx_hash,
                                    chain_id,
                                )
                            )
                else:
                    await record_settlement(usage_event.id, 0, "", "failed")
                    await enqueue_failed_settlement(
                        usage_event.id,
                        org_id,
                        run_id,
                        "x402",
                        cost_usdc,
                        payment_payload=str(billing.payment_payload) if billing.payment_payload else None,
                    )
                    logger.warning(
                        "Settlement failed run_id=%s: %s",
                        run_id,
                        billing_settled.error,
                    )

        # ── Record marketplace tool earnings for subscribed tools ────────
        await _record_marketplace_earnings(
            mp_by_name=mp_by_name,
            tool_names_used=usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
            caller_org_id=org_id,
        )

        if marketplace_stats_billable:
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


@router.get("/agent/tools", tags=["Agent"])
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
        cost_usdc = tool_overrides.get(qualified_name, tool_overrides.get(tool.name, default_cost))
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
