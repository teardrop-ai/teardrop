# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for the read-only A2A agent discovery tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.definitions.discover_agents import discover_agents

pytestmark = pytest.mark.anyio


def _snapshot() -> dict:
    return {
        "generated_at": "2026-08-30T00:00:00+00:00",
        "agents": [
            {
                "org_slug": "caller",
                "org_name": "Caller",
                "agent_url": "https://caller.example.com/",
                "tool_count": 2,
                "tool_names": ["web_search"],
                "registered_at": "2026-08-01T00:00:00+00:00",
                "reputation_score": 0.9,
                "success_rate": 0.95,
                "sample_size": 8.0,
                "confidence": 0.6,
                "unique_caller_count": 6,
                "last_event_at": "2026-08-29T00:00:00+00:00",
                "is_stale": False,
            },
            {
                "org_slug": "new-agent",
                "org_name": "New Agent",
                "agent_url": "https://new.example.com/",
                "tool_count": 3,
                "tool_names": ["get_yield_rates"],
                "registered_at": "2026-08-30T00:00:00+00:00",
                "reputation_score": None,
                "success_rate": None,
                "sample_size": None,
                "confidence": None,
                "unique_caller_count": None,
                "last_event_at": None,
                "is_stale": None,
                "private_token": "must-not-leak",
            },
            {
                "org_slug": "rated-agent",
                "org_name": "Rated Agent",
                "agent_url": "https://rated.example.com",
                "tool_count": 4,
                "tool_names": ["risk_analysis"],
                "registered_at": "2026-07-01T00:00:00+00:00",
                "reputation_score": 0.8,
                "success_rate": 0.9,
                "sample_size": 10.0,
                "confidence": 0.7,
                "unique_caller_count": 7,
                "last_event_at": "2026-08-29T00:00:00+00:00",
                "is_stale": False,
            },
        ],
    }


async def test_discovery_excludes_own_org_marks_allowlist_and_unrated(monkeypatch):
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {"caller_org_slug": "caller", "agent_url": "https://new.example.com"},
            {"caller_org_slug": "caller", "agent_url": None},
        ]
    )
    monkeypatch.setattr("tools.definitions.discover_agents.get_settings", lambda: SimpleNamespace(marketplace_enabled=True))
    monkeypatch.setattr("marketplace.agents.get_agent_directory", AsyncMock(return_value=_snapshot()))
    monkeypatch.setattr("marketplace.context._get_pool", lambda: pool)

    result = await discover_agents(config={"configurable": {"org_id": "caller-id"}})

    assert [agent["org_slug"] for agent in result["agents"]] == ["new-agent", "rated-agent"]
    assert result["agents"][0]["allowlisted"] is True
    assert result["agents"][0]["reputation"]["status"] == "unrated"
    assert result["agents"][0]["registered_at"] == "2026-08-30T00:00:00+00:00"
    assert result["agents"][1]["allowlisted"] is False
    assert result["agents"][0]["message_endpoint"] == "https://new.example.com/message:send"
    assert result["registration_endpoint"] == "/marketplace/agent-registration"
    assert result["registration_benefits_url"] == "/.well-known/registry-benefits.json"
    assert "must-not-leak" not in json.dumps(result)
    query = pool.fetch.call_args.args[0]
    assert "a2a_allowed_agents" in query
    assert pool.fetch.call_args.args[1] == "caller-id"


async def test_discovery_matches_published_tool_names(monkeypatch):
    monkeypatch.setattr("tools.definitions.discover_agents.get_settings", lambda: SimpleNamespace(marketplace_enabled=True))
    monkeypatch.setattr("marketplace.agents.get_agent_directory", AsyncMock(return_value=_snapshot()))

    result = await discover_agents(q="YIELD")

    assert [agent["org_slug"] for agent in result["agents"]] == ["new-agent"]
    assert result["agents"][0]["tool_names"] == ["get_yield_rates"]


async def test_discovery_is_bounded_and_context_free(monkeypatch):
    snapshot = _snapshot()
    snapshot["agents"] = [
        {
            **snapshot["agents"][1],
            "org_slug": f"agent-{index}",
            "org_name": f"Agent {index}",
            "agent_url": f"https://agent-{index}.example.com",
        }
        for index in range(60)
    ]
    monkeypatch.setattr("tools.definitions.discover_agents.get_settings", lambda: SimpleNamespace(marketplace_enabled=True))
    monkeypatch.setattr("marketplace.agents.get_agent_directory", AsyncMock(return_value=snapshot))

    result = await discover_agents(limit=500)

    assert result["count"] == 50
    assert all(agent["allowlisted"] is None for agent in result["agents"])


async def test_discovery_is_empty_when_marketplace_disabled(monkeypatch):
    monkeypatch.setattr(
        "tools.definitions.discover_agents.get_settings",
        lambda: SimpleNamespace(marketplace_enabled=False, a2a_delegation_enabled=True),
    )

    result = await discover_agents()

    assert result == {"generated_at": None, "count": 0, "agents": []}


async def test_discovery_is_empty_when_a2a_delegation_disabled(monkeypatch):
    monkeypatch.setattr(
        "tools.definitions.discover_agents.get_settings",
        lambda: SimpleNamespace(marketplace_enabled=True, a2a_delegation_enabled=False),
    )

    result = await discover_agents()

    assert result == {"generated_at": None, "count": 0, "agents": []}
