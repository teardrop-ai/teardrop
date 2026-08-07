"""Focused outcome-classification tests for the streaming event loop."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from teardrop.agent_event_loop import stream_graph_events


class _InitialState:
    def model_dump(self) -> dict:
        return {}


class _FailingGraph:
    async def astream_events(self, *_args, **_kwargs):
        raise RuntimeError("internal failure")
        yield {}


class _CancelledGraph:
    async def astream_events(self, *_args, **_kwargs):
        raise asyncio.CancelledError
        yield {}


class _EventGraph:
    def __init__(self, events: list[dict]):
        self.events = events

    async def astream_events(self, *_args, **_kwargs):
        for event in self.events:
            yield event


@pytest.mark.anyio
async def test_stream_error_is_classified_as_failure():
    result: dict[str, object] = {}
    events = [
        event
        async for event in stream_graph_events(
            graph=_FailingGraph(),
            initial_state=_InitialState(),
            config={},
            run_id="run-1",
            settings=SimpleNamespace(app_env="production"),
            org_id="org-1",
            payload={},
            result=result,
        )
    ]

    assert events
    assert result == {"terminated": True, "termination_reason": "failed"}


@pytest.mark.anyio
async def test_stream_cancellation_is_not_classified_as_failure():
    result: dict[str, object] = {}
    events = [
        event
        async for event in stream_graph_events(
            graph=_CancelledGraph(),
            initial_state=_InitialState(),
            config={},
            run_id="run-1",
            settings=SimpleNamespace(app_env="production"),
            org_id="org-1",
            payload={},
            result=result,
        )
    ]

    assert events
    assert result == {"terminated": True, "termination_reason": "cancelled"}


@pytest.mark.anyio
async def test_stream_replaces_buffered_primary_text_on_planner_retry():
    graph = _EventGraph(
        [
            {
                "event": "on_chat_model_stream",
                "run_id": "primary-attempt",
                "metadata": {"langgraph_node": "planner"},
                "data": {"chunk": SimpleNamespace(content="PRIMARY PARTIAL")},
            },
            {
                "event": "on_chat_model_stream",
                "run_id": "fallback-attempt",
                "metadata": {"langgraph_node": "planner"},
                "data": {"chunk": SimpleNamespace(content="RECOVERED")},
            },
            {
                "event": "on_chain_end",
                "name": "planner",
                "data": {
                    "output": {
                        "task_status": "generating_ui",
                        "messages": [SimpleNamespace(content="RECOVERED")],
                        "metadata": {},
                    }
                },
            },
            {"event": "on_chain_end", "name": "ui_generator", "data": {"output": {}}},
        ]
    )

    events = [
        event
        async for event in stream_graph_events(
            graph=graph,
            initial_state=_InitialState(),
            config={},
            run_id="run-1",
            settings=SimpleNamespace(app_env="production"),
            org_id="org-1",
            payload={},
            result={},
        )
    ]

    text = "".join(json.loads(event["data"])["delta"] for event in events if event["event"] == "TEXT_MESSAGE_CONTENT")
    assert text == "RECOVERED"
