# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""API tests for opt-in A2A agent registration and discovery."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

_NOW = datetime.now(timezone.utc)


def _registration():
    return {
        "org_id": "test-org-id",
        "agent_url": "https://agent.example.com",
        "created_at": _NOW,
        "updated_at": _NOW,
    }


@pytest.mark.anyio
async def test_agent_registration_requires_org_admin(api_client, monkeypatch):
    set_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.set_agent_registration", set_mock)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    response = await api_client.put(
        "/marketplace/agent-registration",
        json={"agent_url": "https://agent.example.com"},
    )

    assert response.status_code == 403
    set_mock.assert_not_awaited()
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_agent_registration_admin_success(admin_api_client, monkeypatch):
    set_mock = AsyncMock(return_value=_registration())
    rate_limit = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.set_agent_registration", set_mock)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", rate_limit)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    response = await admin_api_client.put(
        "/marketplace/agent-registration",
        json={"agent_url": "https://agent.example.com"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "org_id": "test-org-id",
        "agent_url": "https://agent.example.com",
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }
    set_mock.assert_awaited_once_with("test-org-id", "https://agent.example.com")
    rate_limit.assert_awaited_once_with(
        "a2a_registration:test-org-id",
        config.get_settings().rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration.",
    )
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_public_agent_directory_paginates_and_sets_cache_header(anon_client, monkeypatch):
    agents = [
        {
            "org_slug": "alpha",
            "org_name": "Alpha",
            "agent_url": "https://alpha.example.com",
            "tool_count": 2,
            "reputation_score": None,
            "success_rate": None,
            "sample_size": None,
            "confidence": None,
            "unique_caller_count": None,
        },
        {
            "org_slug": "beta",
            "org_name": "Beta",
            "agent_url": "https://beta.example.com",
            "tool_count": 1,
            "reputation_score": 0.9,
            "success_rate": 0.95,
            "sample_size": 20.0,
            "confidence": 0.8,
            "unique_caller_count": 5,
        },
    ]
    directory = AsyncMock(return_value={"generated_at": _NOW.isoformat(), "agents": agents})
    monkeypatch.setattr("teardrop.routers.marketplace.get_agent_directory", directory)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    first = await anon_client.get("/marketplace/agents?limit=1")

    assert first.status_code == 200
    assert first.headers["cache-control"] == "public, max-age=60"
    assert first.json()["agents"][0]["org_slug"] == "alpha"
    assert first.json()["agents"][0]["agent_card_url"] == "https://alpha.example.com/.well-known/agent-card.json"
    assert first.json()["agents"][0]["message_endpoint"] == "https://alpha.example.com/message:send"
    assert first.json()["next_cursor"]

    second = await anon_client.get(f"/marketplace/agents?cursor={first.json()['next_cursor']}")

    assert second.status_code == 200
    assert [agent["org_slug"] for agent in second.json()["agents"]] == ["beta"]
    assert second.json()["agents"][0]["success_rate"] == 0.95
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_public_agent_directory_rejects_invalid_cursor(anon_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    response = await anon_client.get("/marketplace/agents?cursor=not-a-valid-cursor")

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid agent directory cursor."
    config.get_settings.cache_clear()
