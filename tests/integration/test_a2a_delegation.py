"""Integration tests for A2A delegation — end-to-end with a mock A2A server."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from a2a_client import (
    A2AAgentCard,
    A2ASendMessageResponse,
    A2ATask,
    A2ATaskStatus,
)
from tools.definitions.delegate_to_agent import delegate_to_agent

_MOD = "a2a_client"


@pytest.mark.anyio
async def test_end_to_end_delegation(test_settings, monkeypatch):
    """Full tool invocation: card discovery → message send → result extraction."""
    import config as _config

    monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
    _config.get_settings.cache_clear()

    mock_card = A2AAgentCard(
        name="Math Agent",
        description="Solves math problems",
        url="https://math-agent.example.com",
    )
    mock_response = A2ASendMessageResponse(
        task=A2ATask(
            id="task-int-001",
            status=A2ATaskStatus(
                state="completed",
            ),
            artifacts=[],
        ),
        raw={},
    )

    with (
        patch(f"{_MOD}.validate_url", return_value=None),
        patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
        patch(f"{_MOD}.send_message", AsyncMock(return_value=mock_response)),
        patch(f"{_MOD}.extract_result_text", return_value="The answer is 12."),
    ):
        result = await delegate_to_agent(
            "https://math-agent.example.com",
            "What is the square root of 144?",
        )

    assert result["agent_name"] == "Math Agent"
    assert result["status"] == "completed"
    assert result["result"] == "The answer is 12."
    assert result["error"] is None


@pytest.mark.anyio
async def test_delegation_with_failed_remote_agent(test_settings, monkeypatch):
    """Remote agent returns failed state — error field is populated."""
    import config as _config

    monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
    _config.get_settings.cache_clear()

    mock_card = A2AAgentCard(name="Failing Agent", description="Will fail")
    mock_response = A2ASendMessageResponse(
        task=A2ATask(
            id="task-int-002",
            status=A2ATaskStatus(state="failed"),
            artifacts=[],
        ),
        raw={},
    )

    with (
        patch(f"{_MOD}.validate_url", return_value=None),
        patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
        patch(f"{_MOD}.send_message", AsyncMock(return_value=mock_response)),
        patch(f"{_MOD}.extract_result_text", return_value=""),
    ):
        result = await delegate_to_agent(
            "https://failing-agent.example.com",
            "Do something that will fail",
        )

    assert result["status"] == "failed"
    assert result["error"] is not None
    assert result["agent_name"] == "Failing Agent"


@pytest.mark.anyio
async def test_delegation_network_error(test_settings, monkeypatch):
    """Network-level failure is returned as a tool error, not an exception."""
    import config as _config

    monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
    _config.get_settings.cache_clear()

    mock_card = A2AAgentCard(name="Unreachable Agent", description="Cannot reach")

    err = ConnectionError("Connection refused")
    with (
        patch(f"{_MOD}.validate_url", return_value=None),
        patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
        patch(f"{_MOD}.send_message", AsyncMock(side_effect=err)),
    ):
        result = await delegate_to_agent(
            "https://unreachable-agent.example.com",
            "Try to reach me",
        )

    assert result["status"] == "failed"
    assert "Connection refused" in result["error"]


_AGENT_URL = "https://jwt-agent.example.com"
_JWT = "eyJhbGciOiJSUzI1NiJ9.test-token"
_DELEGATION_MOD = "tools.definitions.delegate_to_agent"


@pytest.mark.anyio
async def test_jwt_forwarded_when_rule_enabled(test_settings, monkeypatch):
    """When jwt_forward=True in allowlist rule, send_message receives the caller's JWT."""
    import config as _config

    monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
    _config.get_settings.cache_clear()

    mock_card = A2AAgentCard(name="Secure Agent", description="Requires JWT", url=_AGENT_URL)
    mock_response = A2ASendMessageResponse(
        task=A2ATask(id="task-jwt-001", status=A2ATaskStatus(state="completed"), artifacts=[]),
        raw={},
    )
    agent_rule = {
        "id": "rule-1",
        "agent_url": _AGENT_URL,
        "label": "secure",
        "max_cost_usdc": 0,
        "require_x402": False,
        "jwt_forward": True,
        "created_at": None,
    }

    mock_send = AsyncMock(return_value=mock_response)

    with (
        patch(f"{_MOD}.validate_url", return_value=None),
        patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
        patch(f"{_MOD}.send_message", mock_send),
        patch(f"{_MOD}.extract_result_text", return_value="OK"),
        patch(f"{_DELEGATION_MOD}.check_delegation_allowed", AsyncMock(return_value=(True, agent_rule))),
    ):
        result = await delegate_to_agent(
            _AGENT_URL,
            "Secure task",
            config={"configurable": {"org_id": "org-1", "db_pool": object(), "jwt_token": _JWT}},
        )

    assert result["status"] == "completed"
    mock_send.assert_awaited_once()
    _, kwargs = mock_send.call_args
    assert kwargs.get("auth_header") == _JWT


@pytest.mark.anyio
async def test_jwt_not_forwarded_when_rule_disabled(test_settings, monkeypatch):
    """When jwt_forward=False in allowlist rule, send_message receives auth_header=None."""
    import config as _config

    monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
    _config.get_settings.cache_clear()

    mock_card = A2AAgentCard(name="Open Agent", description="No JWT needed", url=_AGENT_URL)
    mock_response = A2ASendMessageResponse(
        task=A2ATask(id="task-jwt-002", status=A2ATaskStatus(state="completed"), artifacts=[]),
        raw={},
    )
    agent_rule = {
        "id": "rule-2",
        "agent_url": _AGENT_URL,
        "label": "open",
        "max_cost_usdc": 0,
        "require_x402": False,
        "jwt_forward": False,
        "created_at": None,
    }

    mock_send = AsyncMock(return_value=mock_response)

    with (
        patch(f"{_MOD}.validate_url", return_value=None),
        patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
        patch(f"{_MOD}.send_message", mock_send),
        patch(f"{_MOD}.extract_result_text", return_value="OK"),
        patch(f"{_DELEGATION_MOD}.check_delegation_allowed", AsyncMock(return_value=(True, agent_rule))),
    ):
        result = await delegate_to_agent(
            _AGENT_URL,
            "Open task",
            config={"configurable": {"org_id": "org-1", "db_pool": object(), "jwt_token": _JWT}},
        )

    assert result["status"] == "completed"
    mock_send.assert_awaited_once()
    _, kwargs = mock_send.call_args
    assert kwargs.get("auth_header") is None


@pytest.mark.anyio
async def test_jwt_forward_true_but_no_token_sends_no_auth(test_settings, monkeypatch):
    """When jwt_forward=True but no JWT present (non-user caller), auth_header is None."""
    import config as _config

    monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
    _config.get_settings.cache_clear()

    mock_card = A2AAgentCard(name="Secure Agent", description="Requires JWT", url=_AGENT_URL)
    mock_response = A2ASendMessageResponse(
        task=A2ATask(id="task-jwt-003", status=A2ATaskStatus(state="completed"), artifacts=[]),
        raw={},
    )
    agent_rule = {
        "id": "rule-3",
        "agent_url": _AGENT_URL,
        "label": "secure",
        "max_cost_usdc": 0,
        "require_x402": False,
        "jwt_forward": True,
        "created_at": None,
    }

    mock_send = AsyncMock(return_value=mock_response)

    with (
        patch(f"{_MOD}.validate_url", return_value=None),
        patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
        patch(f"{_MOD}.send_message", mock_send),
        patch(f"{_MOD}.extract_result_text", return_value="OK"),
        patch(f"{_DELEGATION_MOD}.check_delegation_allowed", AsyncMock(return_value=(True, agent_rule))),
    ):
        result = await delegate_to_agent(
            _AGENT_URL,
            "Secure task without JWT",
            config={"configurable": {"org_id": "org-1", "db_pool": object(), "jwt_token": None}},
        )

    assert result["status"] == "completed"
    mock_send.assert_awaited_once()
    _, kwargs = mock_send.call_args
    assert kwargs.get("auth_header") is None
