# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""LangGraph event-dispatch loop for the agent run endpoint.

Extracted verbatim from ``teardrop.routers.agent``'s inner ``_stream``
generator. ``stream_graph_events`` drives ``graph.astream_events`` and yields
the per-token / per-tool / per-surface SSE frames (RUN_STARTED, USAGE_SUMMARY,
BILLING_SETTLEMENT, RUN_FINISHED and DONE remain the caller's responsibility).

The generator mutates the supplied ``result`` dict to set ``result["terminated"]``
and a reason when it short-circuits on cancellation or an unhandled exception,
so callers can preserve accounting behavior while classifying outcomes safely.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from teardrop.agent_stream import (
    _EV_CUSTOM,
    _EV_ERROR,
    _EV_SURFACE_UPDATE,
    _EV_TEXT_MSG_CONTENT,
    _EV_TEXT_MSG_END,
    _EV_TEXT_MSG_START,
    _EV_TOOL_CALL_END,
    _EV_TOOL_CALL_START,
    _A2UIStreamFilter,
    _recover_planner_suffix,
    _should_flush_planner_buffer,
    _sse_event,
)

logger = logging.getLogger(__name__)


def _coerce_stream_text(content: Any) -> str:
    """Normalise provider-specific content blocks to plain text."""
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
            for block in content
            if (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) == "text"
        )
    return content if isinstance(content, str) else str(content or "")


async def stream_graph_events(
    *,
    graph: Any,
    initial_state: Any,
    config: dict[str, Any],
    run_id: str,
    settings: Any,
    org_id: Any,
    payload: dict[str, Any],
    result: dict[str, Any],
):
    """Drive ``graph.astream_events`` and yield SSE frames.

    On normal completion ``result["terminated"]`` stays ``False``. On
    cancellation or an unhandled exception the matching error frame is yielded,
    ``result["terminated"]`` and ``result["termination_reason"]`` are set,
    and the generator returns so the caller can skip usage accounting.
    """
    result["terminated"] = False
    result["termination_reason"] = ""

    # Streaming a2ui-block scrubber for planner text tokens. ui_generator
    # also calls an LLM (raw JSON output); we additionally guard by
    # langgraph_node so its tokens never reach TEXT_MESSAGE_CONTENT.
    _text_filter = _A2UIStreamFilter()
    _last_msg_id: str = run_id
    _planner_token_buffer: list[tuple[str, str]] = []
    _text_emitted = False

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
        result["terminated"] = True
        result["termination_reason"] = "cancelled"
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
        result["terminated"] = True
        result["termination_reason"] = "failed"
        return
