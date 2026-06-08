"""Unit tests for agent/graph.py routing functions.

Tests the conditional routing logic without building or running the full graph.
No Postgres checkpointer or LangGraph runtime is needed.
"""

from __future__ import annotations

from types import SimpleNamespace
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
        """Below the cap (3 of 4) the agent should keep planning."""
        state = AgentState(
            messages=[HumanMessage(content="test")],
            task_status=TaskStatus.PLANNING,
            metadata={"_usage": {"tool_iterations": 3}},
        )
        assert _route_after_tools(state) == "planner"

    def test_planning_routes_to_ui_generator_when_limit_reached(self, test_settings):
        """Exactly at the default cap (4) the agent must stop looping."""
        state = AgentState(
            messages=[HumanMessage(content="test")],
            task_status=TaskStatus.PLANNING,
            metadata={"_usage": {"tool_iterations": 4}},
        )
        assert _route_after_tools(state) == "ui_generator"

    def test_planning_routes_to_ui_generator_when_limit_exceeded(self, test_settings):
        """Beyond the cap (off-by-one guard) must also stop."""
        state = AgentState(
            messages=[HumanMessage(content="test")],
            task_status=TaskStatus.PLANNING,
            metadata={"_usage": {"tool_iterations": 5}},
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

    def test_planning_with_incomplete_plan_routes_to_planner(self, test_settings):
        state = AgentState.model_validate(
            {
                "messages": [HumanMessage(content="test")],
                "task_status": TaskStatus.PLANNING,
                "metadata": {},
                "plan": {
                    "stages": [{"stage_id": 1, "calls": [{"call_id": "c1", "tool": "x", "args": {}, "depends_on": []}]}],
                    "current_stage_index": 0,
                },
            }
        )
        assert _route_after_tools(state) == "planner"

    def test_planning_with_completed_plan_routes_to_ui(self, test_settings):
        state = AgentState.model_validate(
            {
                "messages": [HumanMessage(content="test")],
                "task_status": TaskStatus.PLANNING,
                "metadata": {},
                "plan": {
                    "stages": [{"stage_id": 1, "calls": [{"call_id": "c1", "tool": "x", "args": {}, "depends_on": []}]}],
                    "current_stage_index": 1,
                },
            }
        )
        assert _route_after_tools(state) == "ui_generator"


# ─── close_checkpointer ───────────────────────────────────────────────────────


@pytest.mark.anyio
class TestInitCheckpointer:
    async def test_init_uses_validating_connection_pool(self):
        class DummyPool:
            @staticmethod
            async def check_connection(conn):
                return None

            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        class DummySaver:
            def __init__(self, conn):
                self.conn = conn
                self.setup = AsyncMock()

        mock_stack = AsyncMock()
        mock_stack.enter_async_context = AsyncMock(side_effect=lambda cm: cm)

        with (
            patch.object(graph_module, "AsyncExitStack", return_value=mock_stack),
            patch.object(graph_module, "AsyncConnectionPool", DummyPool),
            patch.object(graph_module, "AsyncPostgresSaver", DummySaver),
            patch.object(
                graph_module,
                "get_settings",
                return_value=SimpleNamespace(pg_dsn="postgresql://user:pass@host/db"),
            ),
            patch.object(graph_module, "_checkpointer", None),
            patch.object(graph_module, "_checkpointer_pool", None),
            patch.object(graph_module, "_exit_stack", None),
        ):
            saver = await graph_module.init_checkpointer()

        assert isinstance(saver, DummySaver)
        assert isinstance(saver.conn, DummyPool)
        assert saver.conn.kwargs["conninfo"] == "postgresql://user:pass@host/db"
        assert saver.conn.kwargs["min_size"] == 1
        assert saver.conn.kwargs["max_size"] == 1
        assert saver.conn.kwargs["open"] is False
        assert saver.conn.kwargs["check"] is DummyPool.check_connection
        assert saver.conn.kwargs["name"] == "langgraph-checkpointer"
        assert saver.conn.kwargs["kwargs"]["autocommit"] is True
        assert saver.conn.kwargs["kwargs"]["prepare_threshold"] == 0
        assert saver.conn.kwargs["kwargs"]["row_factory"] is graph_module.dict_row
        mock_stack.enter_async_context.assert_awaited_once_with(saver.conn)
        saver.setup.assert_awaited_once()


@pytest.mark.anyio
class TestCloseCheckpointer:
    async def test_close_is_noop_when_no_exit_stack(self):
        with (
            patch.object(graph_module, "_exit_stack", None),
            patch.object(graph_module, "_checkpointer", None),
            patch.object(graph_module, "_checkpointer_pool", None),
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
            patch.object(graph_module, "_checkpointer_pool", MagicMock()),
            patch.object(graph_module, "_compiled_graph", MagicMock()),
        ):
            await graph_module.close_checkpointer()
        mock_stack.aclose.assert_called_once()
        assert graph_module._checkpointer is None
        assert graph_module._checkpointer_pool is None
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


# ─── node config injection (regression) ───────────────────────────────────────


class TestNodeConfigInjection:
    """Regression guard for LangGraph runtime-config injection into graph nodes.

    agent/nodes.py and agent/node_executor.py use ``from __future__ import
    annotations`` (PEP 563), which stringifies type hints. LangGraph 1.x detects
    the config-injection parameter by inspecting the raw annotation object, so a
    typed ``config`` hint (``dict | None`` or a stringified ``RunnableConfig``)
    silently disables injection — the planner/executor then read an empty
    ``_org_tools`` and org/MCP/marketplace tools disappear from the inventory.
    The node ``config`` params must stay UNANNOTATED so name-based injection
    applies. This test fails if a future change re-adds a breaking annotation.
    """

    async def test_planner_receives_org_tools_from_injected_config(self, test_settings, monkeypatch):
        from langchain_core.tools import StructuredTool
        from langgraph.checkpoint.memory import InMemorySaver

        import agent.nodes as nodes
        from agent.graph import build_graph

        def _price(symbol: str = "ETH") -> str:
            return "ok"

        org_tool = StructuredTool.from_function(func=_price, name="crypto_price", description="Get crypto price")

        captured: dict[str, list[str]] = {}

        class _StopProbe(Exception):
            pass

        def _capture(state, all_tools, settings, **kwargs):
            captured["tool_names"] = [getattr(t, "name", "") for t in all_tools]
            raise _StopProbe()

        # Capture the tool set the planner would bind, then halt before the LLM call.
        monkeypatch.setattr(nodes, "_resolve_planner_llm", _capture)

        graph = build_graph(InMemorySaver())
        config = {
            "configurable": {
                "thread_id": "u:regression-thread",
                "_org_tools": [org_tool],
                "_org_tools_by_name": {"crypto_price": org_tool},
            }
        }
        init = AgentState(
            messages=[HumanMessage(content="price of eth?")],
            metadata={"_usage": {"tool_iterations": 0}},
        )

        with pytest.raises(_StopProbe):
            async for _ in graph.astream_events(init.model_dump(), config=config, version="v2"):
                pass

        assert "crypto_price" in captured.get("tool_names", []), (
            "planner_node did not receive _org_tools via injected config; ensure the "
            "`config` parameter stays unannotated (PEP 563 breaks typed RunnableConfig injection)"
        )
