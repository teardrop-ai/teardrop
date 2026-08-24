# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for delegate_to_agent JWT forwarding and allowlist enforcement."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import teardrop.config as config


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


_MOCK_CARD = MagicMock()
_MOCK_CARD.name = "TestAgent"

_MOCK_RESPONSE = MagicMock()
_MOCK_RESPONSE.task = MagicMock()
_MOCK_RESPONSE.task.status.state = "completed"
_MOCK_RESPONSE.task.artifacts = []
_MOCK_RESPONSE.task.status.message = None
_MOCK_RESPONSE.task.history = []
_MOCK_RESPONSE.raw = {}


@pytest.mark.asyncio
async def test_jwt_forward_enabled():
    """When allowlist has jwt_forward=true and jwt_token exists, auth_header is passed."""
    agent_url = "https://agent.example.com"

    with (
        patch("teardrop.a2a_client.validate_url", return_value=None),
        patch(
            "teardrop.a2a_client.check_delegation_allowed",
            AsyncMock(
                return_value=(
                    True,
                    {
                        "jwt_forward": True,
                        "max_cost_usdc": 0,
                        "require_x402": False,
                    },
                )
            ),
        ),
        patch("teardrop.a2a_client.discover_agent_card", AsyncMock(return_value=_MOCK_CARD)),
        patch("teardrop.a2a_client.send_message", AsyncMock(return_value=_MOCK_RESPONSE)) as mock_send,
        patch("teardrop.a2a_client.extract_result_text", return_value="ok"),
    ):
        from tools.definitions.delegate_to_agent import delegate_to_agent

        result = await delegate_to_agent(
            agent_url=agent_url,
            task_description="do something",
            config={
                "configurable": {
                    "org_id": "org-1",
                    "run_id": "run-1",
                    "db_pool": MagicMock(),
                    "jwt_token": "my-secret-jwt",
                }
            },
        )

    assert result["status"] == "completed"
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs.get("auth_header") == "my-secret-jwt"


@pytest.mark.anyio
async def test_jwt_forward_disabled():
    """When jwt_forward=false, no auth_header is passed."""
    agent_url = "https://agent.example.com"

    with (
        patch("teardrop.a2a_client.validate_url", return_value=None),
        patch(
            "teardrop.a2a_client.check_delegation_allowed",
            AsyncMock(
                return_value=(
                    True,
                    {
                        "jwt_forward": False,
                        "max_cost_usdc": 0,
                        "require_x402": False,
                    },
                )
            ),
        ),
        patch("teardrop.a2a_client.discover_agent_card", AsyncMock(return_value=_MOCK_CARD)),
        patch("teardrop.a2a_client.send_message", AsyncMock(return_value=_MOCK_RESPONSE)) as mock_send,
        patch("teardrop.a2a_client.extract_result_text", return_value="ok"),
    ):
        from tools.definitions.delegate_to_agent import delegate_to_agent

        result = await delegate_to_agent(
            agent_url=agent_url,
            task_description="do something",
            config={
                "configurable": {
                    "org_id": "org-1",
                    "run_id": "run-1",
                    "db_pool": MagicMock(),
                    "jwt_token": "my-secret-jwt",
                }
            },
        )

    assert result["status"] == "completed"
    assert mock_send.call_args.kwargs.get("auth_header") is None


@pytest.mark.anyio
async def test_allowlist_enforced(monkeypatch):
    """When require_allowlist=true and agent not listed, return error."""
    monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "true")
    config.get_settings.cache_clear()

    agent_url = "https://agent.example.com"

    with (
        patch("teardrop.a2a_client.validate_url", return_value=None),
        patch("teardrop.a2a_client.check_delegation_allowed", AsyncMock(return_value=(False, None))),
    ):
        from tools.definitions.delegate_to_agent import delegate_to_agent

        result = await delegate_to_agent(
            agent_url=agent_url,
            task_description="do something",
            config={
                "configurable": {
                    "org_id": "org-1",
                    "run_id": "run-1",
                    "db_pool": MagicMock(),
                }
            },
        )

    assert result["status"] == "failed"
    assert "not in your organisation" in result["error"]


@pytest.mark.anyio
async def test_allowlist_not_enforced(monkeypatch):
    """When require_allowlist=false and agent not listed, proceed anyway."""
    monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "false")
    config.get_settings.cache_clear()

    agent_url = "https://agent.example.com"

    with (
        patch("teardrop.a2a_client.validate_url", return_value=None),
        patch("teardrop.a2a_client.check_delegation_allowed", AsyncMock(return_value=(False, None))),
        patch("teardrop.a2a_client.discover_agent_card", AsyncMock(return_value=_MOCK_CARD)),
        patch("teardrop.a2a_client.send_message", AsyncMock(return_value=_MOCK_RESPONSE)),
        patch("teardrop.a2a_client.extract_result_text", return_value="ok"),
    ):
        from tools.definitions.delegate_to_agent import delegate_to_agent

        result = await delegate_to_agent(
            agent_url=agent_url,
            task_description="do something",
            config={
                "configurable": {
                    "org_id": "org-1",
                    "run_id": "run-1",
                    "db_pool": MagicMock(),
                }
            },
        )

    assert result["status"] == "completed"


@pytest.mark.anyio
async def test_missing_org_context_fails_closed(monkeypatch):
    """The security-first allowlist default must not be bypassed by direct invocation."""
    monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "true")
    config.get_settings.cache_clear()

    from tools.definitions.delegate_to_agent import delegate_to_agent

    with patch("teardrop.a2a_client.validate_url", return_value=None):
        result = await delegate_to_agent(
            agent_url="https://agent.example.com",
            task_description="do something",
        )

    assert result["status"] == "failed"
    assert "authenticated organisation context" in result["error"]


@pytest.mark.anyio
async def test_allowlist_lookup_failure_fails_closed():
    """A database failure while checking trust must not fall through to dispatch."""
    agent_url = "https://agent.example.com"

    with (
        patch("teardrop.a2a_client.validate_url", return_value=None),
        patch(
            "teardrop.a2a_client.check_delegation_allowed",
            AsyncMock(side_effect=RuntimeError("database credentials leaked")),
        ),
    ):
        from tools.definitions.delegate_to_agent import delegate_to_agent

        result = await delegate_to_agent(
            agent_url=agent_url,
            task_description="do something",
            config={"configurable": {"org_id": "org-1", "db_pool": MagicMock()}},
        )

    assert result["status"] == "failed"
    assert "allowlist" in result["error"].lower()
    assert "database credentials" not in result["error"]
