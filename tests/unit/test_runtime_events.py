from types import SimpleNamespace

import pytest

from agent.runtime_events import RuntimeEventKind, iter_runtime_events


class _InitialState:
    def model_dump(self) -> dict:
        return {"messages": []}


class _Graph:
    def __init__(self, events: list[dict]):
        self.events = events

    async def astream_events(self, *_args, **_kwargs):
        for event in self.events:
            yield event


@pytest.mark.anyio
async def test_adapter_suppresses_non_planner_tokens():
    graph = _Graph(
        [
            {
                "event": "on_chat_model_stream",
                "run_id": "ui-run",
                "metadata": {"langgraph_node": "ui_generator"},
                "data": {"chunk": SimpleNamespace(content="secret-json")},
            },
            {
                "event": "on_chat_model_stream",
                "run_id": "planner-run",
                "metadata": {"langgraph_node": "planner"},
                "data": {"chunk": SimpleNamespace(content="visible")},
            },
        ]
    )

    events = [event async for event in iter_runtime_events(graph, _InitialState(), {})]

    assert len(events) == 1
    assert events[0].kind == RuntimeEventKind.TOKEN
    assert events[0].run_id == "planner-run"
    assert events[0].value.content == "visible"


@pytest.mark.anyio
async def test_adapter_normalizes_tool_and_node_events():
    graph = _Graph(
        [
            {"event": "on_tool_start", "run_id": "call-1", "name": "search", "data": {"input": {"q": "x"}}},
            {"event": "on_tool_end", "run_id": "call-1", "name": "search", "data": {"output": "ok"}},
            {"event": "on_chain_end", "name": "planner", "data": {"output": {"task_status": "completed"}}},
        ]
    )

    events = [event async for event in iter_runtime_events(graph, _InitialState(), {})]

    assert [event.kind for event in events] == [
        RuntimeEventKind.TOOL_START,
        RuntimeEventKind.TOOL_END,
        RuntimeEventKind.NODE_END,
    ]
    assert events[0].value == {"q": "x"}
    assert events[2].node == "planner"
