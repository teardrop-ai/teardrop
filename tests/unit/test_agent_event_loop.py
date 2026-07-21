"""Focused outcome-classification tests for the streaming event loop."""

from __future__ import annotations

import asyncio
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
