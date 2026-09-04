# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""discover_agents - enumerate opt-in A2A agents from the local directory."""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from teardrop.config import get_settings
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_MAX_AGENTS = 50


class DiscoverAgentsInput(BaseModel):
    q: str | None = Field(
        default=None,
        max_length=200,
        description="Optional search across agent name, organization slug, or published tool name",
    )
    limit: int = Field(default=20, ge=1, le=_MAX_AGENTS, description="Maximum number of agents to return")


class AgentReputation(BaseModel):
    status: Literal["rated", "unrated"]
    score: float | None = None
    success_rate: float | None = None
    sample_size: float | None = None
    confidence: float | None = None
    unique_caller_count: int | None = None
    last_event_at: str | None = None
    is_stale: bool | None = None


class DiscoveredAgent(BaseModel):
    org_slug: str
    org_name: str
    agent_url: str
    agent_card_url: str
    message_endpoint: str
    catalog_endpoint: str
    tool_count: int
    tool_names: list[str] = Field(
        default_factory=list,
        description="Names of up to 20 active published tools exposed by the agent organization",
    )
    registered_at: str | None = Field(
        default=None,
        description="UTC timestamp when the organization first registered its A2A endpoint",
    )
    allowlisted: bool | None = Field(
        description="Whether the caller's org has this endpoint in its allowlist; null when caller context is unavailable"
    )
    reputation: AgentReputation


class DiscoverAgentsOutput(BaseModel):
    generated_at: str | None
    count: int
    agents: list[DiscoveredAgent]


def _agent_url_key(value: Any) -> str:
    return str(value or "").rstrip("/")


async def _load_allowlist_context(org_id: str) -> tuple[str | None, set[str] | None]:
    if not org_id:
        return None, None

    try:
        from marketplace.context import _get_pool

        rows = await _get_pool().fetch(
            """
            SELECT org.slug AS caller_org_slug, allowed.agent_url
            FROM orgs AS org
            LEFT JOIN a2a_allowed_agents AS allowed ON allowed.org_id = org.id
            WHERE org.id = $1
            """,
            org_id,
        )
    except Exception as exc:
        logger.warning(
            "discover_agents: allowlist lookup failed org=%s error_type=%s",
            org_id,
            type(exc).__name__,
        )
        return None, None

    caller_org_slug: str | None = None
    allowed_urls: set[str] = set()
    for row in rows:
        if caller_org_slug is None and row.get("caller_org_slug"):
            caller_org_slug = str(row["caller_org_slug"])
        agent_url = _agent_url_key(row.get("agent_url"))
        if agent_url:
            allowed_urls.add(agent_url)
    return caller_org_slug, allowed_urls


async def discover_agents(
    q: str | None = None,
    limit: int = 20,
    *,
    config: dict | None = None,
) -> dict[str, Any]:
    """Return opt-in remote A2A agents without making network requests."""
    settings = get_settings()
    if not settings.marketplace_enabled or not getattr(settings, "a2a_delegation_enabled", True):
        return {"generated_at": None, "count": 0, "agents": []}

    org_id = ""
    if config:
        configurable = config.get("configurable", {})
        if isinstance(configurable, dict):
            org_id = str(configurable.get("org_id", "") or "")

    caller_org_slug, allowed_urls = await _load_allowlist_context(org_id)
    search = str(q or "").strip().casefold()
    snapshot = await _get_directory()
    discovered: list[dict[str, Any]] = []
    for raw_agent in snapshot.get("agents", []):
        if not isinstance(raw_agent, dict):
            continue
        org_slug = str(raw_agent.get("org_slug", "") or "")
        org_name = str(raw_agent.get("org_name", "") or "")
        tool_names = [str(tool_name) for tool_name in (raw_agent.get("tool_names") or []) if tool_name]
        if not org_slug or org_slug == caller_org_slug:
            continue
        if search and not any(search in searchable_value.casefold() for searchable_value in [org_slug, org_name, *tool_names]):
            continue

        agent_url = _agent_url_key(raw_agent.get("agent_url"))
        if not agent_url:
            continue
        reputation_score = raw_agent.get("reputation_score")
        discovered.append(
            {
                "org_slug": org_slug,
                "org_name": org_name,
                "agent_url": agent_url,
                "agent_card_url": f"{agent_url}/.well-known/agent-card.json",
                "message_endpoint": f"{agent_url}/message:send",
                "catalog_endpoint": f"{agent_url}/marketplace/catalog",
                "tool_count": max(0, int(raw_agent.get("tool_count", 0) or 0)),
                "tool_names": tool_names,
                "registered_at": raw_agent.get("registered_at"),
                "allowlisted": None if allowed_urls is None else agent_url in allowed_urls,
                "reputation": {
                    "status": "rated" if reputation_score is not None else "unrated",
                    "score": reputation_score,
                    "success_rate": raw_agent.get("success_rate"),
                    "sample_size": raw_agent.get("sample_size"),
                    "confidence": raw_agent.get("confidence"),
                    "unique_caller_count": raw_agent.get("unique_caller_count"),
                    "last_event_at": raw_agent.get("last_event_at"),
                    "is_stale": raw_agent.get("is_stale"),
                },
            }
        )
        if len(discovered) >= max(1, min(int(limit), _MAX_AGENTS)):
            break

    return {
        "generated_at": snapshot.get("generated_at"),
        "count": len(discovered),
        "agents": discovered,
    }


async def _get_directory() -> dict[str, Any]:
    from marketplace.agents import get_agent_directory

    return await get_agent_directory()


TOOL = ToolDefinition(
    name="discover_agents",
    version="1.0.0",
    description="Find opt-in remote A2A agents, published tool capabilities, endpoints, and public reputation status.",
    tags=["a2a", "agents", "marketplace", "discovery"],
    use_when="Use before delegate_to_agent when a specialist agent is needed but its URL is unknown.",
    limitations="Discovery is read-only and does not add an agent to the org allowlist; delegate_to_agent remains authoritative.",
    alternatives=["delegate_to_agent"],
    input_schema=DiscoverAgentsInput,
    output_schema=DiscoverAgentsOutput,
    annotations={"readOnlyHint": True},
    implementation=discover_agents,
)
