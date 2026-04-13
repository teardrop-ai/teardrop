# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""delegate_to_agent – send a task to a remote A2A-compliant agent."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)


# ─── Schemas ──────────────────────────────────────────────────────────────────


class DelegateToAgentInput(BaseModel):
    agent_url: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Base URL of the remote A2A agent (e.g. https://agent.example.com)",
    )
    task_description: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Natural language description of the task to delegate",
    )


class DelegateToAgentOutput(BaseModel):
    agent_name: str = Field(description="Name of the remote agent (from its agent card)")
    status: str = Field(description="A2A task state: completed, failed, etc.")
    result: str = Field(description="Text result extracted from the remote agent's response")
    error: str | None = Field(default=None, description="Error message, if any")


# ─── Implementation ──────────────────────────────────────────────────────────


async def delegate_to_agent(agent_url: str, task_description: str) -> dict[str, Any]:
    """Delegate a task to a remote A2A agent and return the result.

    This tool discovers the remote agent's capabilities via its published agent
    card, sends a message using the A2A HTTP+JSON/REST binding, and returns the
    agent's response.
    """
    from config import get_settings

    settings = get_settings()

    # ── Feature flag ──────────────────────────────────────────────────────
    if not settings.a2a_delegation_enabled:
        return {
            "agent_name": "unknown",
            "status": "failed",
            "result": "",
            "error": "A2A delegation is not enabled. Set A2A_DELEGATION_ENABLED=true.",
        }

    # ── SSRF check ────────────────────────────────────────────────────────
    from a2a_client import validate_url

    ssrf_err = validate_url(agent_url)
    if ssrf_err:
        return {
            "agent_name": "unknown",
            "status": "failed",
            "result": "",
            "error": f"Blocked URL: {ssrf_err}",
        }

    # ── Discover agent card ───────────────────────────────────────────────
    from a2a_client import discover_agent_card, extract_result_text, send_message

    try:
        card = await discover_agent_card(
            agent_url,
            timeout=min(10, settings.a2a_delegation_timeout_seconds),
            cache_ttl=settings.a2a_agent_card_cache_ttl_seconds,
        )
    except Exception as exc:
        logger.warning("delegate_to_agent: agent card discovery failed for %s: %s", agent_url, exc)
        return {
            "agent_name": "unknown",
            "status": "failed",
            "result": "",
            "error": f"Could not discover agent card at {agent_url}: {exc}",
        }

    # ── Send message ──────────────────────────────────────────────────────
    try:
        response = await send_message(
            agent_url,
            task_description,
            timeout=settings.a2a_delegation_timeout_seconds,
        )
    except Exception as exc:
        logger.warning("delegate_to_agent: message send failed for %s: %s", agent_url, exc)
        return {
            "agent_name": card.name,
            "status": "failed",
            "result": "",
            "error": f"Failed to send message to {card.name}: {exc}",
        }

    # ── Extract result ────────────────────────────────────────────────────
    task_state = "completed"
    if response.task:
        task_state = response.task.status.state

    result_text = extract_result_text(response)

    return {
        "agent_name": card.name,
        "status": task_state,
        "result": result_text,
        "error": None if task_state in ("completed",) else f"Remote agent state: {task_state}",
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="delegate_to_agent",
    version="1.0.0",
    description=(
        "Delegate a task to a remote A2A-compliant agent. Discovers the agent's "
        "capabilities via its agent card, sends it a message, and returns the result. "
        "Use when a task requires specialist capabilities beyond your own tools."
    ),
    tags=["a2a", "delegation", "agent"],
    input_schema=DelegateToAgentInput,
    output_schema=DelegateToAgentOutput,
    implementation=delegate_to_agent,
)
