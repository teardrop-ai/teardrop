# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for opt-in A2A agent registration and directory metrics."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from marketplace.agents import (
    _build_agent_cursor,
    _decode_agent_cursor,
    _load_agent_directory,
    preview_agent_registration,
    probe_agent_registration,
    set_agent_registration,
)
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


def _patch_valid_card(monkeypatch, card: A2AAgentCard | None = None, *, owner_org_id=None) -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=owner_org_id)
    pool.fetchrow = AsyncMock()
    discover = AsyncMock(return_value=card or _a2a_card())
    monkeypatch.setattr("marketplace.agents._get_pool", lambda: pool)
    monkeypatch.setattr("marketplace.agents.async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr("marketplace.agents.discover_agent_card", discover)
    return pool, discover


@pytest.mark.anyio
async def test_preview_agent_registration_bypasses_cache_and_never_writes(monkeypatch):
    card = A2AAgentCard(name="Priced", endpoints={"a2a_message": "/message:send"}, price_per_task_usdc=10_000)
    pool, discover = _patch_valid_card(monkeypatch, card)

    result = await preview_agent_registration("org-1", "HTTPS://AGENT.EXAMPLE.COM/")

    assert result == {
        "registrable": True,
        "detail": None,
        "agent_url": "https://agent.example.com",
        "price_per_task_usdc": 10_000,
    }
    assert discover.await_args.kwargs["bypass_cache"] is True
    pool.fetchrow.assert_not_awaited()


@pytest.mark.anyio
async def test_preview_agent_registration_matches_put_verdicts(monkeypatch):
    _patch_valid_card(monkeypatch, owner_org_id="other-org")
    taken = await preview_agent_registration("org-1", "https://agent.example.com")
    assert taken["registrable"] is False
    assert taken["detail"] == "Agent endpoint is already registered by another organization."

    insecure = await preview_agent_registration("org-1", "http://agent.example.com")
    assert insecure["registrable"] is False
    assert "HTTPS" in insecure["detail"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("offers", "expected_status", "passed"),
    [([10_000], "pass", True), ([8_000, 12_000], "warn", True), ([12_000], "fail", False), ([], "fail", False)],
)
async def test_probe_priced_agent_checks_unpaid_402_offer(monkeypatch, offers, expected_status, passed):
    card = A2AAgentCard(name="Priced", endpoints={"a2a_message": "/message:send"}, price_per_task_usdc=10_000)
    _patch_valid_card(monkeypatch, card)
    probe = AsyncMock(return_value=MagicMock(status_code=402))
    monkeypatch.setattr("teardrop.a2a_client.probe_message_endpoint", probe)
    monkeypatch.setattr("teardrop.a2a_client.exact_payment_offer_amounts", MagicMock(return_value=offers))
    monkeypatch.setattr("teardrop.a2a_client._sign_x402_payment", MagicMock(side_effect=AssertionError("signed")))

    result = await probe_agent_registration("org-1", "https://agent.example.com")

    assert result["passed"] is passed
    assert result["checks"][-1]["name"] == "payment"
    assert result["checks"][-1]["status"] == expected_status
    probe.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "passed"),
    [
        ({"id": "t1", "status": {"state": "completed"}}, True),
        ({"id": "t1", "status": {"state": "working"}}, False),
        ({"ok": True}, False),
    ],
)
async def test_probe_unpriced_agent_requires_completed_task(monkeypatch, body, passed):
    _patch_valid_card(monkeypatch)
    response = MagicMock(status_code=200)
    response.json.return_value = body
    monkeypatch.setattr("teardrop.a2a_client.probe_message_endpoint", AsyncMock(return_value=response))

    result = await probe_agent_registration("org-1", "https://agent.example.com")

    assert result["passed"] is passed
    assert result["checks"][-1]["name"] == "message"


@pytest.mark.anyio
async def test_probe_priced_agent_without_402_warns(monkeypatch):
    card = A2AAgentCard(name="Priced", endpoints={"a2a_message": "/message:send"}, price_per_task_usdc=10_000)
    _patch_valid_card(monkeypatch, card)
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "t1", "status": {"state": "completed"}}
    monkeypatch.setattr("teardrop.a2a_client.probe_message_endpoint", AsyncMock(return_value=response))

    result = await probe_agent_registration("org-1", "https://agent.example.com")

    assert result["passed"] is True
    assert [check["status"] for check in result["checks"]] == ["pass", "pass", "warn"]


@pytest.mark.anyio
async def test_load_agent_directory_suppresses_small_and_derives_reputation(monkeypatch):
    now = datetime.now(timezone.utc)
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "org_id": "org-1",
                "agent_url": "https://trusted.example.com",
                "registered_at": now,
                "org_slug": "trusted",
                "org_name": "Trusted Agent",
                "tool_count": 3,
                "tool_names": ["get_yield_rates", "risk_analysis"],
                "weighted_successes": 8,
                "weighted_sample_size": 10,
                "unique_caller_count": 5,
                "last_event_at": now,
            },
            {
                "org_id": "org-2",
                "agent_url": "https://new.example.com",
                "registered_at": now,
                "org_slug": "new",
                "org_name": "New Agent",
                "tool_count": 0,
                "tool_names": [],
                "weighted_successes": 10,
                "weighted_sample_size": 10,
                "unique_caller_count": 4,
                "last_event_at": now,
            },
            {
                "org_id": "org-3",
                "agent_url": "https://stale.example.com",
                "registered_at": now,
                "org_slug": "stale",
                "org_name": "Stale Agent",
                "tool_count": 1,
                "tool_names": ["web_search"],
                "weighted_successes": 1,
                "weighted_sample_size": 1,
                "unique_caller_count": 5,
                "last_event_at": now - timedelta(days=61),
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
    assert trusted["tool_names"] == ["get_yield_rates", "risk_analysis"]
    assert trusted["registered_at"] == now.isoformat()
    assert trusted["last_event_at"] == now.isoformat()
    assert trusted["is_stale"] is False
    assert snapshot["agents"][1]["success_rate"] is None
    assert snapshot["agents"][1]["last_event_at"] is None
    assert snapshot["agents"][1]["is_stale"] is None
    assert snapshot["agents"][2]["is_stale"] is True
    assert json.loads(json.dumps(snapshot)) == snapshot
    sql = pool.fetch.call_args.args[0]
    assert "a2a_agent_registry" in sql
    assert "MIN(r.created_at) AS registered_at" in sql
    assert "published_tool.name" in sql
    assert "LIMIT 20" in sql
    assert "rtrim(e.agent_url, '/') = r.agent_url" in sql
    assert "e.failure_origin <> 'local'" in sql
    assert "e.task_status <> 'possibly_delivered'" in sql
    assert "e.org_id IS DISTINCT FROM r.org_id" in sql


def test_agent_cursor_supports_sort_keys_and_legacy_name_tokens():
    agent = {"org_slug": "alpha", "reputation_score": 0.91}

    assert _decode_agent_cursor(_build_agent_cursor(agent, "name")) == ("name", "alpha", "alpha", "all")
    assert _decode_agent_cursor(_build_agent_cursor(agent, "reputation")) == ("reputation", 0.91, "alpha", "all")

    legacy = base64.urlsafe_b64encode(json.dumps({"org_slug": "alpha"}).encode()).decode().rstrip("=")
    assert _decode_agent_cursor(legacy) == ("name", "alpha", "alpha", "all")


def test_agent_cursor_normalizes_invalid_reputation_keys():
    for value in ("not-a-number", True, float("nan")):
        agent = {"org_slug": "bad", "reputation_score": value}
        assert _decode_agent_cursor(_build_agent_cursor(agent, "reputation")) == ("reputation", None, "bad", "all")

    malformed_sort = (
        base64.urlsafe_b64encode(json.dumps({"sort": ["reputation"], "key": 0.9, "org_slug": "bad", "stale": "all"}).encode())
        .decode()
        .rstrip("=")
    )
    missing_key = (
        base64.urlsafe_b64encode(json.dumps({"sort": "reputation", "org_slug": "bad", "stale": "all"}).encode())
        .decode()
        .rstrip("=")
    )
    assert _decode_agent_cursor(malformed_sort) is None
    assert _decode_agent_cursor(missing_key) is None
