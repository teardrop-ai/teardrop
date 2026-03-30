"""Compiled LangGraph agent graph for Teardrop.

Graph topology:
  START → planner → [tools if needed] → ui_generator → END
"""

from __future__ import annotations

import functools
import logging
from typing import Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agent.nodes import planner_node, tool_executor_node, ui_generator_node
from agent.state import AgentState, TaskStatus

logger = logging.getLogger(__name__)


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


def build_graph() -> StateGraph:
    """Build and compile the agent StateGraph with a MemorySaver checkpointer."""
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

    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)


@functools.lru_cache(maxsize=1)
def get_graph() -> StateGraph:
    """Return the cached compiled graph singleton."""
    logger.info("Compiling LangGraph agent graph")
    return build_graph()
