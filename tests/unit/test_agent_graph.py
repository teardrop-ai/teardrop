"""Unit tests for agent/graph.py routing functions.

Tests the conditional routing logic without building or running the full graph.
No Postgres checkpointer or LangGraph runtime is needed.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from agent.graph import _route_after_planner, _route_after_tools
from agent.state import AgentState, TaskStatus

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _state(status: TaskStatus) -> AgentState:
    return AgentState(
        messages=[HumanMessage(content="test")],
        task_status=status,
        metadata={},
    )


# ─── _route_after_planner ─────────────────────────────────────────────────────


class TestRouteAfterPlanner:
    def test_executing_routes_to_tool_executor(self):
        state = _state(TaskStatus.EXECUTING)
        assert _route_after_planner(state) == "tool_executor"

    def test_generating_ui_routes_to_ui_generator(self):
        state = _state(TaskStatus.GENERATING_UI)
        assert _route_after_planner(state) == "ui_generator"

    def test_failed_routes_to_ui_generator(self):
        """Any non-EXECUTING status should fall through to ui_generator."""
        state = _state(TaskStatus.FAILED)
        assert _route_after_planner(state) == "ui_generator"

    def test_idle_routes_to_ui_generator(self):
        state = _state(TaskStatus.IDLE)
        assert _route_after_planner(state) == "ui_generator"


# ─── _route_after_tools ───────────────────────────────────────────────────────


class TestRouteAfterTools:
    def test_planning_routes_back_to_planner(self):
        state = _state(TaskStatus.PLANNING)
        assert _route_after_tools(state) == "planner"

    def test_generating_ui_routes_to_ui_generator(self):
        state = _state(TaskStatus.GENERATING_UI)
        assert _route_after_tools(state) == "ui_generator"

    def test_completed_routes_to_ui_generator(self):
        state = _state(TaskStatus.COMPLETED)
        assert _route_after_tools(state) == "ui_generator"

    def test_failed_routes_to_ui_generator(self):
        state = _state(TaskStatus.FAILED)
        assert _route_after_tools(state) == "ui_generator"
