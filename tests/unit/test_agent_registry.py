# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for opt-in A2A agent registration and directory metrics."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from marketplace.agents import _load_agent_directory, set_agent_registration
from teardrop.a2a_client import A2AAgentCard


def _a2a_card() -> A2AAgentCard:
    return A2AAgentCard(name="Remote Agent", endpoints={"a2a_message": "/message:send"})


@pytest.mark.anyio
async def test_set_agent_registration_normalizes_and_upserts(monkeypatch):
    now = datetime.now(timezone.utc)
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "org_id": "org-1",
            "agent_url": "https://agent.example.com",
            "created_at": now,
            "updated_at": now,
        }
    )
    monkeypatch.setattr("marketplace.agents._get_pool", lambda: pool)
    monkeypatch.setattr("marketplace.agents.async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr("marketplace.agents.discover_agent_card", AsyncMock(return_value=_a2a_card()))

    result = await set_agent_registration("org-1", " HTTPS://AGENT.EXAMPLE.COM:443/// ")

    assert result["agent_url"] == "https://agent.example.com"
    assert pool.fetchrow.await_args.args[2] == "https://agent.example.com"
    assert "ON CONFLICT (org_id) DO UPDATE" in pool.fetchrow.await_args.args[0]


@pytest.mark.anyio
async def test_set_agent_registration_rejects_non_https_before_network(monkeypatch):
    validate = AsyncMock(return_value=None)
    discover = AsyncMock()
    monkeypatch.setattr("marketplace.agents.async_validate_url", validate)
    monkeypatch.setattr("marketplace.agents.discover_agent_card", discover)

    with pytest.raises(ValueError, match="HTTPS") as exc_info:
        await set_agent_registration("org-1", "http://agent.example.com")

    assert "agent.example.com" not in str(exc_info.value)
    validate.assert_not_awaited()
    discover.assert_not_awaited()


@pytest.mark.anyio
async def test_set_agent_registration_rejects_card_without_message_endpoint(monkeypatch):
    pool = MagicMock()
    pool.fetchrow = AsyncMock()
    monkeypatch.setattr("marketplace.agents._get_pool", lambda: pool)
    monkeypatch.setattr("marketplace.agents.async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "marketplace.agents.discover_agent_card",
        AsyncMock(return_value=A2AAgentCard(name="MCP Only Agent")),
    )

    with pytest.raises(ValueError, match="message endpoint"):
        await set_agent_registration("org-1", "https://agent.example.com")

    pool.fetchrow.assert_not_awaited()


@pytest.mark.anyio
async def test_set_agent_registration_accepts_matching_absolute_message_endpoint(monkeypatch):
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "org_id": "org-1",
            "agent_url": "https://agent.example.com",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    monkeypatch.setattr("marketplace.agents._get_pool", lambda: pool)
    monkeypatch.setattr("marketplace.agents.async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "marketplace.agents.discover_agent_card",
        AsyncMock(
            return_value=A2AAgentCard(
                name="Standard Agent",
                supportedInterfaces=[{"url": "HTTPS://AGENT.EXAMPLE.COM:443/message:send"}],
            )
        ),
    )

    result = await set_agent_registration("org-1", "https://agent.example.com")

    assert result["org_id"] == "org-1"
    pool.fetchrow.assert_awaited_once()


@pytest.mark.anyio
async def test_set_agent_registration_rejects_message_endpoint_on_other_host(monkeypatch):
    pool = MagicMock()
    pool.fetchrow = AsyncMock()
    monkeypatch.setattr("marketplace.agents._get_pool", lambda: pool)
    monkeypatch.setattr("marketplace.agents.async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "marketplace.agents.discover_agent_card",
        AsyncMock(
            return_value=A2AAgentCard(
                name="Mismatched Agent",
                supportedInterfaces=[{"url": "https://other.example.com/message:send"}],
            )
        ),
    )

    with pytest.raises(ValueError, match="message endpoint"):
        await set_agent_registration("org-1", "https://agent.example.com")

    pool.fetchrow.assert_not_awaited()


@pytest.mark.anyio
async def test_load_agent_directory_suppresses_small_and_derives_reputation(monkeypatch):
    now = datetime.now(timezone.utc)
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "org_id": "org-1",
                "agent_url": "https://trusted.example.com",
                "org_slug": "trusted",
                "org_name": "Trusted Agent",
                "tool_count": 3,
                "weighted_successes": 8,
                "weighted_sample_size": 10,
                "unique_caller_count": 5,
                "last_event_at": now,
            },
            {
                "org_id": "org-2",
                "agent_url": "https://new.example.com",
                "org_slug": "new",
                "org_name": "New Agent",
                "tool_count": 0,
                "weighted_successes": 10,
                "weighted_sample_size": 10,
                "unique_caller_count": 4,
                "last_event_at": now,
            },
        ]
    )
    monkeypatch.setattr("marketplace.agents._get_pool", lambda: pool)

    snapshot = await _load_agent_directory()

    trusted = snapshot["agents"][0]
    assert trusted["success_rate"] == pytest.approx(12 / 15, abs=0.000001)
    assert trusted["confidence"] == pytest.approx(10 / 15, abs=0.000001)
    assert trusted["unique_caller_count"] == 5
    assert trusted["reputation_score"] is not None
    assert snapshot["agents"][1]["success_rate"] is None
    sql = pool.fetch.call_args.args[0]
    assert "a2a_agent_registry" in sql
    assert "rtrim(e.agent_url, '/') = r.agent_url" in sql
    assert "e.failure_origin <> 'local'" in sql
    assert "e.task_status <> 'possibly_delivered'" in sql
    assert "e.org_id IS DISTINCT FROM r.org_id" in sql
