# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Compiled LangGraph agent graph for Teardrop.

Graph topology:
  START → planner → [tools if needed] → ui_generator → END
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Literal

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from agent.nodes import planner_node, tool_executor_node, ui_generator_node
from agent.state import AgentState, TaskStatus
from config import get_settings

logger = logging.getLogger(__name__)

# Module-level handles so the lifespan can set/close them.
_checkpointer: AsyncPostgresSaver | None = None
_exit_stack: AsyncExitStack | None = None


async def init_checkpointer() -> AsyncPostgresSaver:
    """Create and store the async Postgres checkpointer (via psycopg3)."""
    global _checkpointer, _exit_stack
    settings = get_settings()
    _exit_stack = AsyncExitStack()
    _checkpointer = await _exit_stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(settings.pg_dsn)
    )
    await _checkpointer.setup()
    logger.info("Postgres checkpointer ready")
    return _checkpointer


async def close_checkpointer() -> None:
    """Gracefully close the checkpointer connection pool."""
    global _checkpointer, _exit_stack, _compiled_graph
    _compiled_graph = None
    _checkpointer = None
    if _exit_stack is not None:
        await _exit_stack.aclose()
        _exit_stack = None
        logger.info("Postgres checkpointer closed")


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


def build_graph(checkpointer: AsyncPostgresSaver) -> StateGraph:
    """Build and compile the agent StateGraph with an async Postgres checkpointer."""
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
_graph_lock: asyncio.Lock | None = None


def _get_graph_lock() -> asyncio.Lock:
    global _graph_lock
    if _graph_lock is None:
        _graph_lock = asyncio.Lock()
    return _graph_lock


async def get_graph() -> StateGraph:
    """Return the compiled graph, building it once on first call."""
    global _compiled_graph
    async with _get_graph_lock():
        if _compiled_graph is None:
            if _checkpointer is None:
                raise RuntimeError("Checkpointer not initialized. Call init_checkpointer() first.")
            _compiled_graph = build_graph(_checkpointer)
            logger.info("Compiled LangGraph agent graph")
    return _compiled_graph
