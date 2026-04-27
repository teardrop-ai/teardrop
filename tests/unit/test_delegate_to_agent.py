"""Unit tests for tools/definitions/delegate_to_agent.py.

No real HTTP calls are made; a2a_client functions and config are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from a2a_client import (
    A2AAgentCard,
    A2ASendMessageResponse,
    A2ATask,
    A2ATaskStatus,
)
from tools.definitions.delegate_to_agent import (
    DelegateToAgentInput,
    DelegateToAgentOutput,
    delegate_to_agent,
)

_MOD = "a2a_client"


# ─── Input schema validation ──────────────────────────────────────────────────


class TestDelegateToAgentInput:
    def test_valid_input(self):
        inp = DelegateToAgentInput(
            agent_url="https://agent.example.com",
            task_description="Summarise this text",
        )
        assert inp.agent_url == "https://agent.example.com"

    def test_agent_url_too_short(self):
        with pytest.raises(ValidationError):
            DelegateToAgentInput(agent_url="http://x", task_description="test")

    def test_agent_url_too_long(self):
        with pytest.raises(ValidationError):
            DelegateToAgentInput(agent_url="https://x" * 300, task_description="test")

    def test_task_description_empty(self):
        with pytest.raises(ValidationError):
            DelegateToAgentInput(agent_url="https://agent.example.com", task_description="")

    def test_task_description_max_length(self):
        with pytest.raises(ValidationError):
            DelegateToAgentInput(
                agent_url="https://agent.example.com",
                task_description="x" * 4097,
            )


# ─── Output schema ────────────────────────────────────────────────────────────


class TestDelegateToAgentOutput:
    def test_valid_output(self):
        out = DelegateToAgentOutput(
            agent_name="TestAgent",
            status="completed",
            result="Done",
            error=None,
        )
        assert out.agent_name == "TestAgent"

    def test_output_with_error(self):
        out = DelegateToAgentOutput(
            agent_name="TestAgent",
            status="failed",
            result="",
            error="Remote agent state: failed",
        )
        assert out.error is not None


# ─── delegate_to_agent implementation ─────────────────────────────────────────


class TestDelegateToAgent:
    async def test_disabled_returns_error(self, test_settings, monkeypatch):
        """When A2A_DELEGATION_ENABLED is false, tool returns an error."""
        import config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "false")
        _config.get_settings.cache_clear()

        result = await delegate_to_agent("https://agent.example.com", "test task")
        assert result["status"] == "failed"
        assert "not enabled" in result["error"]

    async def test_ssrf_blocked(self, test_settings, monkeypatch):
        """SSRF-blocked URLs return a tool error, not an exception."""
        import config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        _config.get_settings.cache_clear()

        result = await delegate_to_agent("https://192.168.1.1", "test task")
        assert result["status"] == "failed"
        assert "Blocked" in result["error"]

    async def test_agent_card_discovery_failure(self, test_settings, monkeypatch):
        """When agent card cannot be fetched, a descriptive error is returned."""
        import config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        _config.get_settings.cache_clear()

        with (
            patch(f"{_MOD}.validate_url", return_value=None),
            patch(
                f"{_MOD}.discover_agent_card",
                AsyncMock(side_effect=Exception("Connection refused")),
            ),
        ):
            result = await delegate_to_agent(
                "https://agent.example.com",
                "test task",
            )
            assert result["status"] == "failed"
            assert "Connection refused" in result["error"]

    async def test_happy_path(self, test_settings, monkeypatch):
        """Successful delegation returns agent name, status, and result text."""
        import config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        _config.get_settings.cache_clear()

        mock_card = A2AAgentCard(name="SpecialistAgent", description="A specialist")
        mock_response = A2ASendMessageResponse(
            task=A2ATask(
                id="task-001",
                status=A2ATaskStatus(state="completed"),
                artifacts=[],
            ),
            raw={},
        )

        with (
            patch(f"{_MOD}.validate_url", return_value=None),
            patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_MOD}.send_message", AsyncMock(return_value=mock_response)),
            patch(f"{_MOD}.extract_result_text", return_value="The answer is 42"),
        ):
            result = await delegate_to_agent(
                "https://agent.example.com",
                "What is the meaning of life?",
            )
            assert result["agent_name"] == "SpecialistAgent"
            assert result["status"] == "completed"
            assert result["result"] == "The answer is 42"
            assert result["error"] is None

    async def test_remote_agent_failure(self, test_settings, monkeypatch):
        """When remote agent returns a failed task, error field is populated."""
        import config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        _config.get_settings.cache_clear()

        mock_card = A2AAgentCard(name="FailAgent", description="Will fail")
        mock_response = A2ASendMessageResponse(
            task=A2ATask(
                id="task-002",
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
                "https://agent.example.com",
                "do something",
            )
            assert result["status"] == "failed"
            assert result["error"] is not None

    async def test_send_message_exception(self, test_settings, monkeypatch):
        """HTTP-level failure during message send returns a tool error."""
        import config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        _config.get_settings.cache_clear()

        mock_card = A2AAgentCard(name="TimeoutAgent", description="Will timeout")

        with (
            patch(f"{_MOD}.validate_url", return_value=None),
            patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_MOD}.send_message", AsyncMock(side_effect=Exception("Timeout"))),
        ):
            result = await delegate_to_agent(
                "https://agent.example.com",
                "do something",
            )
            assert result["status"] == "failed"
            assert "Timeout" in result["error"]
