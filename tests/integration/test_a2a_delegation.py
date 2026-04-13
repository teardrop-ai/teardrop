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
