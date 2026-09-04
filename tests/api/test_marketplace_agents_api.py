# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""API tests for opt-in A2A agent registration and discovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
async def test_agent_registration_requires_org_machine_or_admin(api_client, monkeypatch):
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
async def test_agent_registration_machine_credentials_success(anon_client, monkeypatch):
    from teardrop.auth import create_access_token

    set_mock = AsyncMock(return_value=_registration())
    rate_limit = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.set_agent_registration", set_mock)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", rate_limit)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    token = create_access_token(
        subject="machine-client",
        extra_claims={"auth_method": "client_credentials", "org_id": "test-org-id"},
    )
    response = await anon_client.put(
        "/marketplace/agent-registration",
        json={"agent_url": "https://agent.example.com"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    set_mock.assert_awaited_once_with("test-org-id", "https://agent.example.com")
    rate_limit.assert_awaited_once()
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_agent_registration_unscoped_machine_credentials_forbidden(anon_client, monkeypatch):
    from teardrop.auth import create_access_token

    set_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.set_agent_registration", set_mock)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    token = create_access_token(
        subject="config-client",
        extra_claims={"auth_method": "client_credentials", "org_id": ""},
    )
    response = await anon_client.put(
        "/marketplace/agent-registration",
        json={"agent_url": "https://agent.example.com"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    set_mock.assert_not_awaited()
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_agent_registration_machine_credentials_delete(anon_client, monkeypatch):
    from teardrop.auth import create_access_token

    delete_mock = AsyncMock()
    rate_limit = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.delete_agent_registration", delete_mock)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", rate_limit)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    token = create_access_token(
        subject="machine-client",
        extra_claims={"auth_method": "client_credentials", "org_id": "test-org-id"},
    )
    response = await anon_client.delete(
        "/marketplace/agent-registration",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    delete_mock.assert_awaited_once_with("test-org-id")
    rate_limit.assert_awaited_once()
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
            "registered_at": "2026-08-01T00:00:00+00:00",
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
    assert first.json()["agents"][0]["registered_at"] == "2026-08-01T00:00:00+00:00"
    assert first.json()["agents"][0]["last_event_at"] is None
    assert first.json()["agents"][0]["is_stale"] is None
    assert first.json()["next_cursor"]

    second = await anon_client.get(f"/marketplace/agents?cursor={first.json()['next_cursor']}")

    assert second.status_code == 200
    assert [agent["org_slug"] for agent in second.json()["agents"]] == ["beta"]
    assert second.json()["agents"][0]["success_rate"] == 0.95
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_public_agent_directory_sorts_reputation_and_binds_cursor(anon_client, monkeypatch):
    agents = [
        {
            "org_slug": "zeta",
            "org_name": "Zeta",
            "agent_url": "https://zeta.example.com",
            "tool_count": 1,
            "reputation_score": 0.95,
            "success_rate": 0.95,
            "sample_size": 10.0,
            "confidence": 0.7,
            "unique_caller_count": 5,
            "last_event_at": _NOW.isoformat(),
            "is_stale": False,
        },
        {
            "org_slug": "beta",
            "org_name": "Beta",
            "agent_url": "https://beta.example.com",
            "tool_count": 1,
            "reputation_score": 0.9,
            "success_rate": 0.9,
            "sample_size": 10.0,
            "confidence": 0.7,
            "unique_caller_count": 5,
            "last_event_at": _NOW.isoformat(),
            "is_stale": False,
        },
        {
            "org_slug": "alpha",
            "org_name": "Alpha",
            "agent_url": "https://alpha.example.com",
            "tool_count": 1,
            "reputation_score": 0.9,
            "success_rate": 0.9,
            "sample_size": 10.0,
            "confidence": 0.7,
            "unique_caller_count": 5,
            "last_event_at": _NOW.isoformat(),
            "is_stale": False,
        },
        {
            "org_slug": "new",
            "org_name": "New",
            "agent_url": "https://new.example.com",
            "tool_count": 0,
            "reputation_score": None,
            "success_rate": None,
            "sample_size": None,
            "confidence": None,
            "unique_caller_count": None,
            "last_event_at": None,
            "is_stale": None,
        },
    ]
    directory = AsyncMock(return_value={"generated_at": _NOW.isoformat(), "agents": agents})
    monkeypatch.setattr("teardrop.routers.marketplace.get_agent_directory", directory)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    first = await anon_client.get("/marketplace/agents?sort=reputation&limit=2")

    assert first.status_code == 200
    assert [agent["org_slug"] for agent in first.json()["agents"]] == ["zeta", "alpha"]
    assert first.json()["agents"][0]["is_stale"] is not None
    cursor = first.json()["next_cursor"]
    assert cursor

    second = await anon_client.get(f"/marketplace/agents?sort=reputation&cursor={cursor}")

    assert second.status_code == 200
    assert [agent["org_slug"] for agent in second.json()["agents"]] == ["beta", "new"]
    assert second.json()["agents"][1]["reputation_score"] is None

    mismatch = await anon_client.get(f"/marketplace/agents?cursor={cursor}")

    assert mismatch.status_code == 422
    assert mismatch.json()["detail"] == "Agent directory cursor does not match the requested sort."
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_public_agent_directory_filters_staleness(anon_client, monkeypatch):
    agents = [
        {
            "org_slug": "fresh",
            "org_name": "Fresh",
            "agent_url": "https://fresh.example.com",
            "tool_count": 1,
            "reputation_score": 0.9,
            "success_rate": 0.9,
            "sample_size": 10.0,
            "confidence": 0.7,
            "unique_caller_count": 5,
            "last_event_at": _NOW.isoformat(),
            "is_stale": False,
        },
        {
            "org_slug": "old",
            "org_name": "Old",
            "agent_url": "https://old.example.com",
            "tool_count": 1,
            "reputation_score": 0.8,
            "success_rate": 0.8,
            "sample_size": 10.0,
            "confidence": 0.7,
            "unique_caller_count": 5,
            "last_event_at": (_NOW - timedelta(days=61)).isoformat(),
            "is_stale": True,
        },
        {
            "org_slug": "unknown",
            "org_name": "Unknown",
            "agent_url": "https://unknown.example.com",
            "tool_count": 0,
            "reputation_score": None,
            "success_rate": None,
            "sample_size": None,
            "confidence": None,
            "unique_caller_count": None,
            "last_event_at": None,
            "is_stale": None,
        },
    ]
    directory = AsyncMock(return_value={"generated_at": _NOW.isoformat(), "agents": agents})
    monkeypatch.setattr("teardrop.routers.marketplace.get_agent_directory", directory)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    active = await anon_client.get("/marketplace/agents?stale=active")
    stale = await anon_client.get("/marketplace/agents?stale=stale")
    all_agents = await anon_client.get("/marketplace/agents?limit=1")

    assert [agent["org_slug"] for agent in active.json()["agents"]] == ["fresh"]
    assert [agent["org_slug"] for agent in stale.json()["agents"]] == ["old"]
    assert [agent["org_slug"] for agent in all_agents.json()["agents"]] == ["fresh"]
    cursor = all_agents.json()["next_cursor"]
    mismatch = await anon_client.get(f"/marketplace/agents?stale=active&cursor={cursor}")

    assert mismatch.status_code == 422
    assert mismatch.json()["detail"] == "Agent directory cursor does not match the requested stale filter."
    config.get_settings.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("sort=score", "Invalid agent directory sort. Allowed: name, reputation."),
        ("stale=unknown", "Invalid agent directory stale filter. Allowed: active, all, stale."),
    ],
)
async def test_public_agent_directory_rejects_invalid_filters(anon_client, monkeypatch, query, detail):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()
    response = await anon_client.get(f"/marketplace/agents?{query}")

    assert response.status_code == 400
    assert response.json()["detail"] == detail
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
