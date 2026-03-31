"""Compiled LangGraph agent graph for Teardrop.

Graph topology:
  START → planner → [tools if needed] → ui_generator → END
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from agent.nodes import planner_node, tool_executor_node, ui_generator_node
from agent.state import AgentState, TaskStatus
from config import get_settings

logger = logging.getLogger(__name__)

# Module-level handle so the lifespan can set/close it.
_checkpointer: AsyncSqliteSaver | None = None


async def init_checkpointer() -> AsyncSqliteSaver:
    """Create and store the async SQLite checkpointer."""
    global _checkpointer
    settings = get_settings()
    import aiosqlite

    conn = await aiosqlite.connect(settings.checkpoint_db_path)
    _checkpointer = AsyncSqliteSaver(conn)
    await _checkpointer.setup()
    logger.info("SQLite checkpointer ready at %s", settings.checkpoint_db_path)
    return _checkpointer


async def close_checkpointer() -> None:
    """Gracefully close the checkpointer connection."""
    global _checkpointer, _compiled_graph
    if _checkpointer is not None:
        await _checkpointer.conn.close()
        _checkpointer = None
        _compiled_graph = None
        logger.info("SQLite checkpointer closed")


def _route_after_planner(state: AgentState) -> Literal["tool_executor", "ui_generator"]:
    """Decide next node after the planner runs."""
    if state.task_status == TaskStatus.EXECUTING:
        return "tool_executor"
    return "ui_generator"


def _route_after_tools(state: AgentState) -> Literal["planner", "ui_generator"]:
    """After executing tools, loop back to planner for further reasoning."""
    if state.task_status == TaskStatus.PLANNING:
        return "planner"
    return "ui_generator"


def build_graph(checkpointer: AsyncSqliteSaver) -> StateGraph:
    """Build and compile the agent StateGraph with an async SQLite checkpointer."""
    builder = StateGraph(AgentState)

    # Register nodes
    builder.add_node("planner", planner_node)
    builder.add_node("tool_executor", tool_executor_node)
    builder.add_node("ui_generator", ui_generator_node)

    # Entry point
    builder.add_edge(START, "planner")

    # Conditional routing after planner
    builder.add_conditional_edges(
        "planner",
        _route_after_planner,
        {"tool_executor": "tool_executor", "ui_generator": "ui_generator"},
    )

    # After tools, loop back to planner or go to UI generation
    builder.add_conditional_edges(
        "tool_executor",
        _route_after_tools,
        {"planner": "planner", "ui_generator": "ui_generator"},
    )

    # UI generation always terminates
    builder.add_edge("ui_generator", END)

    return builder.compile(checkpointer=checkpointer)


# Cached compiled graph — built once after checkpointer is initialized.
_compiled_graph = None


def get_graph() -> StateGraph:
    """Return the compiled graph, building it once on first call."""
    global _compiled_graph
    if _compiled_graph is None:
        if _checkpointer is None:
            raise RuntimeError("Checkpointer not initialized. Call init_checkpointer() first.")
        _compiled_graph = build_graph(_checkpointer)
        logger.info("Compiled LangGraph agent graph")
    return _compiled_graph
