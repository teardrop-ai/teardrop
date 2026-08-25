# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for tools/definitions/delegate_to_agent.py.

No real HTTP calls are made; a2a_client functions and config are mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from teardrop.a2a_client import (
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

_MOD = "teardrop.a2a_client"
_BILLING_MOD = "billing"


# ─── Input schema validation ──────────────────────────────────────────────────


class TestDelegateToAgentInput:
    def test_valid_input(self):
        inp = DelegateToAgentInput(
            agent_url="https://agent.example.com",
            task_description="Summarise this text",
        )
        assert inp.agent_url == "https://agent.example.com"
        assert inp.task_type == "general"

    def test_task_type_is_bounded(self):
        inp = DelegateToAgentInput(
            agent_url="https://agent.example.com",
            task_description="Research current market conditions",
            task_type="research",
        )
        assert inp.task_type == "research"

        with pytest.raises(ValidationError):
            DelegateToAgentInput(
                agent_url="https://agent.example.com",
                task_description="Research current market conditions",
                task_type="raw prompt text must not be stored",
            )

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
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "false")
        _config.get_settings.cache_clear()

        result = await delegate_to_agent("https://agent.example.com", "test task")
        assert result["status"] == "failed"
        assert "not enabled" in result["error"]

    async def test_ssrf_blocked(self, test_settings, monkeypatch):
        """SSRF-blocked URLs return a tool error, not an exception."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "false")
        _config.get_settings.cache_clear()

        result = await delegate_to_agent("https://192.168.1.1", "test task")
        assert result["status"] == "failed"
        assert "Blocked" in result["error"]

    async def test_agent_card_discovery_failure(self, test_settings, monkeypatch):
        """When agent card cannot be fetched, a descriptive error is returned."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "false")
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
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "false")
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
        import teardrop.config as _config

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
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "false")
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

    async def test_send_message_has_hard_timeout(self, test_settings, monkeypatch):
        """A remote send that never completes is cancelled at the delegation boundary."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "false")
        _config.get_settings.cache_clear()
        settings = _config.get_settings()
        monkeypatch.setattr(settings, "a2a_delegation_timeout_seconds", 0.01)

        mock_card = A2AAgentCard(name="HangingAgent", description="Never responds")
        cancelled = asyncio.Event()

        async def hanging_send(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with (
            patch(f"{_MOD}.validate_url", return_value=None),
            patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_MOD}.send_message", AsyncMock(side_effect=hanging_send)),
        ):
            result = await delegate_to_agent(
                "https://agent.example.com",
                "do something",
                config={"configurable": {"run_id": "timeout-run"}},
            )

        assert result["status"] == "failed"
        assert "Failed to send message to HangingAgent" in result["error"]
        assert "timed out after" in result["error"]
        assert cancelled.is_set()

    async def test_same_run_delegations_respect_concurrency_limit(self, test_settings, monkeypatch):
        """Concurrent remote sends in one run do not exceed the configured permit count."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "false")
        _config.get_settings.cache_clear()
        settings = _config.get_settings()
        monkeypatch.setattr(settings, "a2a_delegation_timeout_seconds", 1)
        monkeypatch.setattr(settings, "a2a_delegation_max_concurrent_per_run", 1)

        mock_card = A2AAgentCard(name="QueuedAgent", description="Handles one request at a time")
        mock_response = A2ASendMessageResponse(
            task=A2ATask(
                id="task-concurrent",
                status=A2ATaskStatus(state="completed"),
                artifacts=[],
            ),
            raw={},
        )
        release = asyncio.Event()
        send_started = asyncio.Event()
        active_sends = 0
        max_active_sends = 0
        send_call_count = 0

        async def controlled_send(*args, **kwargs):
            nonlocal active_sends, max_active_sends, send_call_count
            send_call_count += 1
            active_sends += 1
            max_active_sends = max(max_active_sends, active_sends)
            send_started.set()
            try:
                await release.wait()
                return mock_response
            finally:
                active_sends -= 1

        with (
            patch(f"{_MOD}.validate_url", return_value=None),
            patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_MOD}.send_message", AsyncMock(side_effect=controlled_send)),
            patch(f"{_MOD}.extract_result_text", return_value="done"),
        ):
            tasks = [
                asyncio.create_task(
                    delegate_to_agent(
                        "https://agent.example.com",
                        "do something",
                        config={"configurable": {"run_id": "concurrency-run"}},
                    )
                )
                for _ in range(2)
            ]
            await asyncio.wait_for(send_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert send_call_count == 1
            assert max_active_sends == 1

            release.set()
            results = await asyncio.gather(*tasks)

        assert all(result["status"] == "completed" for result in results)
        assert max_active_sends == 1

    async def test_x402_failure_before_payment_attempt_refunds(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "0")
        _config.get_settings.cache_clear()

        mock_card = A2AAgentCard(name="PaidAgent", description="Requires payment")
        budget_pool = AsyncMock()
        budget_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 100_000, "spending_limit_usdc": 0, "is_paused": False})
        send_paid = AsyncMock(side_effect=RuntimeError("signing failed"))
        fund = AsyncMock(return_value=True)
        record = AsyncMock(return_value=True)
        refund = AsyncMock(return_value=True)
        mark = AsyncMock(return_value=True)
        config = {"configurable": {"org_id": "org-1", "run_id": "run-paid-pre-sign", "db_pool": object()}}

        with (
            patch(f"{_MOD}.validate_url", return_value=None),
            patch(
                f"{_MOD}.check_delegation_allowed",
                AsyncMock(return_value=(True, {"max_cost_usdc": 50_000, "require_x402": True})),
            ),
            patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_MOD}.send_message_with_payment", send_paid),
            patch(f"{_BILLING_MOD}._get_pool", return_value=budget_pool),
            patch(f"{_BILLING_MOD}.fund_delegation", fund),
            patch(f"{_BILLING_MOD}.record_delegation_event", record),
            patch(f"{_BILLING_MOD}.refund_delegation", refund),
            patch(f"{_BILLING_MOD}.mark_delegation_possibly_delivered", mark),
            patch(f"{_BILLING_MOD}.get_treasury_signer", return_value=object()),
        ):
            result = await delegate_to_agent("https://agent.example.com", "do paid work", config=config)

        assert result["status"] == "failed"
        assert result["cost_usdc"] == 0
        mark.assert_not_awaited()
        refund.assert_awaited_once()
        assert record.await_args.kwargs["task_status"] == "failed"

    async def test_x402_timeout_after_payment_attempt_is_possibly_delivered(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "0")
        _config.get_settings.cache_clear()

        mock_card = A2AAgentCard(name="PaidAgent", description="Requires payment")
        budget_pool = AsyncMock()
        budget_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 100_000, "spending_limit_usdc": 0, "is_paused": False})
        mark = AsyncMock(return_value=True)

        async def send_paid(*args, **kwargs):
            await kwargs["payment_attempt_callback"]()
            raise TimeoutError("remote response timed out")

        fund = AsyncMock(return_value=True)
        record = AsyncMock(return_value=True)
        refund = AsyncMock(return_value=True)
        config = {"configurable": {"org_id": "org-1", "run_id": "run-paid-ambiguous", "db_pool": object()}}

        with (
            patch(f"{_MOD}.validate_url", return_value=None),
            patch(
                f"{_MOD}.check_delegation_allowed",
                AsyncMock(return_value=(True, {"max_cost_usdc": 50_000, "require_x402": True})),
            ),
            patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_MOD}.send_message_with_payment", AsyncMock(side_effect=send_paid)),
            patch(f"{_BILLING_MOD}._get_pool", return_value=budget_pool),
            patch(f"{_BILLING_MOD}.fund_delegation", fund),
            patch(f"{_BILLING_MOD}.record_delegation_event", record),
            patch(f"{_BILLING_MOD}.refund_delegation", refund),
            patch(f"{_BILLING_MOD}.mark_delegation_possibly_delivered", mark),
            patch(f"{_BILLING_MOD}.get_treasury_signer", return_value=object()),
        ):
            result = await delegate_to_agent("https://agent.example.com", "do paid work", config=config)

        assert result["status"] == "possibly_delivered"
        assert result["cost_usdc"] == 50_000
        assert "ambiguous" in result["error"]
        mark.assert_awaited_once_with("org-1", fund.await_args.args[4])
        refund.assert_not_awaited()
        assert record.await_args.kwargs["task_status"] == "possibly_delivered"
        assert record.await_args.kwargs["cost_usdc"] == 50_000

    async def test_x402_explicit_failed_task_refunds_via_delivery_resolver(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_REQUIRE_ALLOWLIST", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "0")
        _config.get_settings.cache_clear()

        mock_card = A2AAgentCard(name="PaidAgent", description="Requires payment")
        mock_response = A2ASendMessageResponse(
            task=A2ATask(
                id="task-paid-failed",
                status=A2ATaskStatus(state="failed"),
                artifacts=[],
            ),
            raw={},
            settlement_tx="0x" + "b" * 64,
        )
        budget_pool = AsyncMock()
        budget_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 100_000, "spending_limit_usdc": 0, "is_paused": False})
        fund = AsyncMock(return_value=True)
        record = AsyncMock(return_value=True)
        refund = AsyncMock(return_value=True)
        mark = AsyncMock(return_value=True)
        fail = AsyncMock(return_value=True)
        config = {"configurable": {"org_id": "org-1", "run_id": "run-paid-failed", "db_pool": object()}}

        async def send_paid(*args, **kwargs):
            await kwargs["payment_attempt_callback"]()
            return mock_response

        with (
            patch(f"{_MOD}.validate_url", return_value=None),
            patch(
                f"{_MOD}.check_delegation_allowed",
                AsyncMock(return_value=(True, {"max_cost_usdc": 50_000, "require_x402": True})),
            ),
            patch(f"{_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_MOD}.send_message_with_payment", AsyncMock(side_effect=send_paid)),
            patch(f"{_BILLING_MOD}._get_pool", return_value=budget_pool),
            patch(f"{_BILLING_MOD}.fund_delegation", fund),
            patch(f"{_BILLING_MOD}.record_delegation_event", record),
            patch(f"{_BILLING_MOD}.refund_delegation", refund),
            patch(f"{_BILLING_MOD}.fail_delegation_delivery", fail),
            patch(f"{_BILLING_MOD}.mark_delegation_possibly_delivered", mark),
            patch(f"{_BILLING_MOD}.get_treasury_signer", return_value=object()),
        ):
            result = await delegate_to_agent("https://agent.example.com", "do paid work", config=config)

        assert result["status"] == "failed"
        assert result["cost_usdc"] == 0
        mark.assert_awaited_once()
        fail.assert_awaited_once()
        refund.assert_not_awaited()
        assert record.await_args.kwargs["billing_method"] == "x402"
        assert record.await_args.kwargs["settlement_tx"] == "0x" + "b" * 64
