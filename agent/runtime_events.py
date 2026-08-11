# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Framework-neutral events emitted by the active agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator


class RuntimeEventKind(str, Enum):
    TOKEN = "token"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    NODE_END = "node_end"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    kind: RuntimeEventKind
    run_id: str = ""
    node: str = ""
    value: Any = None


async def iter_runtime_events(graph: Any, initial_state: Any, config: dict[str, Any]) -> AsyncIterator[RuntimeEvent]:
    async for event in graph.astream_events(initial_state.model_dump(), config=config, version="v2"):
        event_name = event.get("event", "")
        event_data = event.get("data", {})
        run_id = event.get("run_id", "")
        node = event.get("name", "")

        if event_name == "on_chat_model_stream":
            if event.get("metadata", {}).get("langgraph_node") == "planner":
                yield RuntimeEvent(RuntimeEventKind.TOKEN, run_id=run_id, node="planner", value=event_data.get("chunk"))
        elif event_name == "on_tool_start":
            yield RuntimeEvent(RuntimeEventKind.TOOL_START, run_id=run_id, node=node, value=event_data.get("input", {}))
        elif event_name == "on_tool_end":
            yield RuntimeEvent(RuntimeEventKind.TOOL_END, run_id=run_id, node=node, value=event_data.get("output", ""))
        elif event_name == "on_chain_end" and node in {"planner", "tool_executor", "ui_generator"}:
            yield RuntimeEvent(RuntimeEventKind.NODE_END, run_id=run_id, node=node, value=event_data.get("output", {}))
