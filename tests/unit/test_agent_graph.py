"""Unit tests for agent/graph.py routing functions.

Tests the conditional routing logic without building or running the full graph.
No Postgres checkpointer or LangGraph runtime is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

import agent.graph as graph_module
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
    def test_planning_routes_back_to_planner(self, test_settings):
        state = _state(TaskStatus.PLANNING)
        assert _route_after_tools(state) == "planner"

    def test_generating_ui_routes_to_ui_generator(self, test_settings):
        state = _state(TaskStatus.GENERATING_UI)
        assert _route_after_tools(state) == "ui_generator"

    def test_completed_routes_to_ui_generator(self, test_settings):
        state = _state(TaskStatus.COMPLETED)
        assert _route_after_tools(state) == "ui_generator"

    def test_failed_routes_to_ui_generator(self, test_settings):
        state = _state(TaskStatus.FAILED)
        assert _route_after_tools(state) == "ui_generator"

    def test_planning_routes_to_planner_when_below_limit(self, test_settings):
        """Below the cap (3 of 12) the agent should keep planning."""
        state = AgentState(
            messages=[HumanMessage(content="test")],
            task_status=TaskStatus.PLANNING,
            metadata={"_usage": {"tool_iterations": 3}},
        )
        assert _route_after_tools(state) == "planner"

    def test_planning_routes_to_ui_generator_when_limit_reached(self, test_settings):
        """Exactly at the default cap (12) the agent must stop looping."""
        state = AgentState(
            messages=[HumanMessage(content="test")],
            task_status=TaskStatus.PLANNING,
            metadata={"_usage": {"tool_iterations": 12}},
        )
        assert _route_after_tools(state) == "ui_generator"

    def test_planning_routes_to_ui_generator_when_limit_exceeded(self, test_settings):
        """Beyond the cap (off-by-one guard) must also stop."""
        state = AgentState(
            messages=[HumanMessage(content="test")],
            task_status=TaskStatus.PLANNING,
            metadata={"_usage": {"tool_iterations": 13}},
        )
        assert _route_after_tools(state) == "ui_generator"

    def test_planning_routes_to_planner_with_no_usage_key(self, test_settings):
        """Missing _usage key should default to 0 iterations and not raise."""
        state = AgentState(
            messages=[HumanMessage(content="test")],
            task_status=TaskStatus.PLANNING,
            metadata={},
        )
        assert _route_after_tools(state) == "planner"


# ─── close_checkpointer ───────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCloseCheckpointer:
    async def test_close_is_noop_when_no_exit_stack(self):
        with (
            patch.object(graph_module, "_exit_stack", None),
            patch.object(graph_module, "_checkpointer", None),
            patch.object(graph_module, "_compiled_graph", None),
        ):
            await graph_module.close_checkpointer()  # must not raise
        assert graph_module._checkpointer is None

    async def test_close_closes_exit_stack(self):
        mock_stack = AsyncMock()
        mock_stack.aclose = AsyncMock()
        with (
            patch.object(graph_module, "_exit_stack", mock_stack),
            patch.object(graph_module, "_checkpointer", MagicMock()),
            patch.object(graph_module, "_compiled_graph", MagicMock()),
        ):
            await graph_module.close_checkpointer()
        mock_stack.aclose.assert_called_once()
        assert graph_module._checkpointer is None
        assert graph_module._exit_stack is None


# ─── get_graph ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetGraph:
    async def test_raises_when_checkpointer_not_initialised(self):
        with (
            patch.object(graph_module, "_compiled_graph", None),
            patch.object(graph_module, "_checkpointer", None),
            patch.object(graph_module, "_graph_lock", None),
        ):
            with pytest.raises(RuntimeError, match="Checkpointer not initialized"):
                await graph_module.get_graph()

    async def test_returns_cached_graph(self):
        mock_graph = MagicMock()
        with patch.object(graph_module, "_compiled_graph", mock_graph):
            result = await graph_module.get_graph()
        assert result is mock_graph


# ─── _get_graph_lock ─────────────────────────────────────────────────────────


class TestGetGraphLock:
    def test_creates_lock_on_first_call(self):
        import asyncio

        with patch.object(graph_module, "_graph_lock", None):
            lock = graph_module._get_graph_lock()
        assert isinstance(lock, asyncio.Lock)

    def test_returns_existing_lock(self):
        import asyncio

        existing = asyncio.Lock()
        with patch.object(graph_module, "_graph_lock", existing):
            lock = graph_module._get_graph_lock()
        assert lock is existing
