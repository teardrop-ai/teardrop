"""Agent state schema shared across all graph nodes."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Lifecycle status of an agent task run."""

    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    GENERATING_UI = "generating_ui"
    COMPLETED = "completed"
    FAILED = "failed"


class A2UIComponent(BaseModel):
    """A single declarative UI component emitted by the agent."""

    type: str = Field(..., description="Component type: text|table|columns|rows|form|button|progress")
    props: dict[str, Any] = Field(default_factory=dict, description="Component properties")
    children: list["A2UIComponent"] = Field(default_factory=list, description="Nested children")

    model_config = {"arbitrary_types_allowed": True}


class AgentState(BaseModel):
    """Full mutable state threaded through the LangGraph workflow."""

    # Core conversation history (append-only via add_messages reducer)
    messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)

    # Task lifecycle
    task_status: TaskStatus = TaskStatus.IDLE

    # A2UI components ready to surface to the frontend
    ui_components: list[A2UIComponent] = Field(default_factory=list)

    # Arbitrary key/value metadata (thread_id, user_id, etc.)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Error information when task_status == FAILED
    error: str | None = None

    model_config = {"arbitrary_types_allowed": True}
