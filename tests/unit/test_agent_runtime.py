# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for teardrop/agent_runtime.py — pre-run context assembly."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from billing import BillingResult
from teardrop.agent_runtime import _RunContext, run_agent_once


@pytest.mark.anyio
class TestPrepareRunContext:
    """Test that _prepare_run_context survives individual gather failures."""

    async def test_org_tools_exception_returns_empty_tools(self, test_settings):
        """When build_org_langchain_tools raises, the agent run must not crash."""
        from teardrop import agent_runtime

        with (
            patch.object(agent_runtime, "get_graph", AsyncMock(return_value=MagicMock())),
            patch.object(agent_runtime, "build_org_langchain_tools", side_effect=RuntimeError("DB down")),
            patch.object(agent_runtime, "build_mcp_langchain_tools", AsyncMock(return_value=([], {}))),
            patch("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {}))),
            patch("teardrop.agent_runtime.recall_memories", AsyncMock(return_value=[])),
            patch("teardrop.agent_runtime.resolve_llm_config", AsyncMock(return_value=None)),
            patch("teardrop.agent_runtime.get_org_by_id", AsyncMock(return_value=MagicMock(name="test-org"))),
            patch("teardrop.agent_runtime.get_credit_balance", AsyncMock(return_value=None)),
            patch.object(test_settings, "memory_enabled", False),
            patch.object(test_settings, "billing_enabled", False),
        ):
            ctx = await agent_runtime._prepare_run_context(
                org_id="org-1",
                user_message="hello",
                billing=MagicMock(verified=False),
                mem_settings=test_settings,
            )

        assert isinstance(ctx, _RunContext)
        assert ctx.org_lc_tools == []
        assert ctx.org_tools_by_name == {}

    async def test_org_tools_exception_logs_warning(self, test_settings, caplog):
        """_safe_org_tools must log a warning, not debug, on failure."""
        import logging

        from teardrop import agent_runtime

        caplog.set_level(logging.WARNING)

        with (
            patch.object(agent_runtime, "get_graph", AsyncMock(return_value=MagicMock())),
            patch.object(agent_runtime, "build_org_langchain_tools", side_effect=RuntimeError("DB down")),
            patch.object(agent_runtime, "build_mcp_langchain_tools", AsyncMock(return_value=([], {}))),
            patch("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {}))),
            patch("teardrop.agent_runtime.recall_memories", AsyncMock(return_value=[])),
            patch("teardrop.agent_runtime.resolve_llm_config", AsyncMock(return_value=None)),
            patch("teardrop.agent_runtime.get_org_by_id", AsyncMock(return_value=MagicMock(name="test-org"))),
            patch("teardrop.agent_runtime.get_credit_balance", AsyncMock(return_value=None)),
            patch.object(test_settings, "memory_enabled", False),
            patch.object(test_settings, "billing_enabled", False),
        ):
            await agent_runtime._prepare_run_context(
                org_id="org-1",
                user_message="hello",
                billing=MagicMock(verified=False),
                mem_settings=test_settings,
            )

        assert any("Org tool discovery failed" in rec.message for rec in caplog.records)

    async def test_logs_tool_inventory_snapshot(self, test_settings, caplog):
        """Telemetry inventory line is emitted with per-source counts."""
        import logging

        from teardrop import agent_runtime

        caplog.set_level(logging.INFO)
        org_tools = [MagicMock(name="org_tool")]
        mcp_tools = [MagicMock(name="mcp_tool")]
        marketplace_tools = [MagicMock(name="market_tool")]

        with (
            patch.object(agent_runtime, "get_graph", AsyncMock(return_value=MagicMock())),
            patch.object(agent_runtime, "build_org_langchain_tools", AsyncMock(return_value=(org_tools, {"org_tool": object()}))),
            patch.object(
                agent_runtime, "build_mcp_langchain_tools", AsyncMock(return_value=(mcp_tools, {"mcp__tool": object()}))
            ),
            patch(
                "marketplace.build_subscribed_marketplace_tools",
                AsyncMock(return_value=(marketplace_tools, {"acme/weather": object()})),
            ),
            patch("teardrop.agent_runtime.recall_memories", AsyncMock(return_value=[])),
            patch("teardrop.agent_runtime.resolve_llm_config", AsyncMock(return_value=None)),
            patch("teardrop.agent_runtime.get_org_by_id", AsyncMock(return_value=MagicMock(name="test-org"))),
            patch("teardrop.agent_runtime.get_credit_balance", AsyncMock(return_value=None)),
            patch.object(test_settings, "memory_enabled", False),
            patch.object(test_settings, "billing_enabled", False),
        ):
            await agent_runtime._prepare_run_context(
                org_id="org-1",
                user_message="hello",
                billing=MagicMock(verified=False),
                mem_settings=test_settings,
            )

        assert any("tool_inventory org_id=org-1 webhook=1 mcp=1 marketplace=1 total=3" in rec.message for rec in caplog.records)


@pytest.mark.anyio
async def test_client_credentials_billing_gate_uses_credit_rail(test_settings, monkeypatch):
    from teardrop import agent_runtime

    test_settings.billing_enabled = True
    test_settings.billable_auth_methods = ["client_credentials"]
    test_settings.credit_min_run_reserve_usdc = 50_000
    monkeypatch.setattr(agent_runtime, "settings", test_settings)
    monkeypatch.setattr(
        agent_runtime,
        "get_current_pricing",
        AsyncMock(return_value=MagicMock(run_price_usdc=10_000)),
    )
    verify_credit_mock = AsyncMock(return_value=BillingResult(verified=True, billing_method="credit"))
    monkeypatch.setattr(agent_runtime, "verify_credit", verify_credit_mock)

    billing_result, gate_response = await agent_runtime._run_billing_gate(
        MagicMock(),
        {"sub": "machine-client", "auth_method": "client_credentials", "org_id": "machine-org"},
        "machine-org",
        is_byok=False,
        platform_fee=0,
    )

    assert gate_response is None
    assert billing_result.verified is True
    assert billing_result.billing_method == "credit"
    verify_credit_mock.assert_awaited_once_with("machine-org", 50_000, principal_id="machine-client")


@pytest.mark.anyio
class TestPromotionalCreditExclusions:
    """Verified-email onboarding credit must not create marketplace author earnings."""

    async def test_prepare_run_context_flags_promotional_credit(self, test_settings):
        """When onboarding credit is enabled and the org is grant-only, flag the run."""
        from teardrop import agent_runtime

        with (
            patch.object(agent_runtime, "get_graph", AsyncMock(return_value=MagicMock())),
            patch.object(agent_runtime, "build_org_langchain_tools", AsyncMock(return_value=([], {}))),
            patch.object(agent_runtime, "build_mcp_langchain_tools", AsyncMock(return_value=([], {}))),
            patch("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {}))),
            patch("teardrop.agent_runtime.recall_memories", AsyncMock(return_value=[])),
            patch("teardrop.agent_runtime.resolve_llm_config", AsyncMock(return_value=None)),
            patch("teardrop.agent_runtime.get_org_by_id", AsyncMock(return_value=MagicMock(name="test-org"))),
            patch("teardrop.agent_runtime.get_credit_balance", AsyncMock(return_value=500_000)),
            patch("teardrop.agent_runtime.is_promotional_credit", AsyncMock(return_value=True)),
            patch.object(test_settings, "memory_enabled", False),
            patch.object(test_settings, "billing_enabled", True),
            patch.object(test_settings, "onboarding_credit_enabled", True),
        ):
            ctx = await agent_runtime._prepare_run_context(
                org_id="org-1",
                user_message="hello",
                billing=BillingResult(verified=True, billing_method="credit"),
                mem_settings=test_settings,
            )

        assert ctx.is_promotional_credit is True

    async def test_prepare_run_context_not_promotional_when_feature_disabled(self, test_settings):
        """Disabling the feature flag must keep runs non-promotional."""
        from teardrop import agent_runtime

        with (
            patch.object(agent_runtime, "get_graph", AsyncMock(return_value=MagicMock())),
            patch.object(agent_runtime, "build_org_langchain_tools", AsyncMock(return_value=([], {}))),
            patch.object(agent_runtime, "build_mcp_langchain_tools", AsyncMock(return_value=([], {}))),
            patch("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {}))),
            patch("teardrop.agent_runtime.recall_memories", AsyncMock(return_value=[])),
            patch("teardrop.agent_runtime.resolve_llm_config", AsyncMock(return_value=None)),
            patch("teardrop.agent_runtime.get_org_by_id", AsyncMock(return_value=MagicMock(name="test-org"))),
            patch("teardrop.agent_runtime.get_credit_balance", AsyncMock(return_value=500_000)),
            patch("teardrop.agent_runtime.is_promotional_credit", AsyncMock(return_value=True)),
            patch.object(test_settings, "memory_enabled", False),
            patch.object(test_settings, "billing_enabled", True),
            patch.object(test_settings, "onboarding_credit_enabled", False),
        ):
            ctx = await agent_runtime._prepare_run_context(
                org_id="org-1",
                user_message="hello",
                billing=BillingResult(verified=True, billing_method="credit"),
                mem_settings=test_settings,
            )

        assert ctx.is_promotional_credit is False

    async def test_prepare_run_context_not_promotional_for_x402(self, test_settings):
        """x402-billed runs must never be treated as promotional credit."""
        from teardrop import agent_runtime

        with (
            patch.object(agent_runtime, "get_graph", AsyncMock(return_value=MagicMock())),
            patch.object(agent_runtime, "build_org_langchain_tools", AsyncMock(return_value=([], {}))),
            patch.object(agent_runtime, "build_mcp_langchain_tools", AsyncMock(return_value=([], {}))),
            patch("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {}))),
            patch("teardrop.agent_runtime.recall_memories", AsyncMock(return_value=[])),
            patch("teardrop.agent_runtime.resolve_llm_config", AsyncMock(return_value=None)),
            patch("teardrop.agent_runtime.get_org_by_id", AsyncMock(return_value=MagicMock(name="test-org"))),
            patch("teardrop.agent_runtime.get_credit_balance", AsyncMock(return_value=500_000)),
            patch("teardrop.agent_runtime.is_promotional_credit", AsyncMock(return_value=True)),
            patch.object(test_settings, "memory_enabled", False),
            patch.object(test_settings, "billing_enabled", True),
            patch.object(test_settings, "onboarding_credit_enabled", True),
        ):
            ctx = await agent_runtime._prepare_run_context(
                org_id="org-1",
                user_message="hello",
                billing=BillingResult(verified=True, billing_method="x402"),
                mem_settings=test_settings,
            )

        assert ctx.is_promotional_credit is False

    async def test_run_agent_once_excludes_marketplace_tools_when_promotional(self, test_settings):
        """Promotional runs must add subscribed marketplace names to _excluded_tool_names."""
        from teardrop import agent_runtime

        graph_mock = MagicMock()
        graph_mock.ainvoke = AsyncMock(return_value={"messages": [], "task_status": "completed"})

        ctx = _RunContext(
            graph=graph_mock,
            org_lc_tools=[],
            org_tools_by_name={},
            mp_by_name={"acme/weather": object(), "platform/web_search": object()},
            recalled=[],
            llm_config=None,
            org_name="test-org",
            credit_balance_usdc=500_000,
            is_promotional_credit=True,
            persisted_excluded_tools=[],
        )

        async def _noop_settlement(*args, **kwargs):
            kwargs["result"]["marketplace_stats_billable"] = True
            yield None

        with (
            patch.object(agent_runtime, "get_settings", return_value=test_settings),
            patch.object(agent_runtime, "_prepare_run_context", AsyncMock(return_value=ctx)),
            patch.object(agent_runtime, "fetch_usage_snapshot", AsyncMock(return_value=(None, {}))),
            patch.object(agent_runtime, "calculate_run_cost", AsyncMock(return_value=0)),
            patch.object(agent_runtime, "record_usage_event", AsyncMock()),
            patch.object(agent_runtime, "dispatch_settlement", _noop_settlement),
            patch.object(agent_runtime, "_record_marketplace_earnings", AsyncMock()) as earnings_mock,
            patch.object(test_settings, "marketplace_enabled", True),
        ):
            result = await run_agent_once(
                org_id="org-1",
                user_id="user-1",
                usage_user_id="user-1",
                usage_org_id="org-1",
                user_message="hello",
                run_id="run-1",
                thread_id="thread-1",
                billing=BillingResult(verified=True, billing_method="credit"),
                is_byok=False,
                org_llm_cfg=None,
                platform_fee=0,
                timeout_seconds=30,
            )

        initial_state = graph_mock.ainvoke.call_args.args[0]
        excluded = set(initial_state.metadata["_excluded_tool_names"])
        assert "acme/weather" in excluded
        assert "platform/web_search" in excluded
        assert result.marketplace_stats_billable is False
        earnings_mock.assert_not_awaited()

    async def test_run_agent_once_preserves_request_and_persisted_exclusions(self, test_settings):
        """Promotional marketplace exclusion is unioned with request and persisted exclusions."""
        from teardrop import agent_runtime

        graph_mock = MagicMock()
        graph_mock.ainvoke = AsyncMock(return_value={"messages": [], "task_status": "completed"})

        ctx = _RunContext(
            graph=graph_mock,
            org_lc_tools=[],
            org_tools_by_name={},
            mp_by_name={"acme/weather": object()},
            recalled=[],
            llm_config=None,
            org_name="test-org",
            credit_balance_usdc=500_000,
            is_promotional_credit=True,
            persisted_excluded_tools=["org/legacy_tool"],
        )

        async def _noop_settlement(*args, **kwargs):
            kwargs["result"]["marketplace_stats_billable"] = True
            yield None

        with (
            patch.object(agent_runtime, "get_settings", return_value=test_settings),
            patch.object(agent_runtime, "_prepare_run_context", AsyncMock(return_value=ctx)),
            patch.object(agent_runtime, "fetch_usage_snapshot", AsyncMock(return_value=(None, {}))),
            patch.object(agent_runtime, "calculate_run_cost", AsyncMock(return_value=0)),
            patch.object(agent_runtime, "record_usage_event", AsyncMock()),
            patch.object(agent_runtime, "dispatch_settlement", _noop_settlement),
            patch.object(agent_runtime, "_record_marketplace_earnings", AsyncMock()),
        ):
            await run_agent_once(
                org_id="org-1",
                user_id="user-1",
                usage_user_id="user-1",
                usage_org_id="org-1",
                user_message="hello",
                run_id="run-1",
                thread_id="thread-1",
                billing=BillingResult(verified=True, billing_method="credit"),
                is_byok=False,
                org_llm_cfg=None,
                platform_fee=0,
                timeout_seconds=30,
                excluded_tool_names=["request/tool"],
            )

        initial_state = graph_mock.ainvoke.call_args.args[0]
        excluded = set(initial_state.metadata["_excluded_tool_names"])
        assert "acme/weather" in excluded
        assert "org/legacy_tool" in excluded
        assert "request/tool" in excluded

    async def test_run_agent_once_allows_marketplace_when_funded(self, test_settings):
        """A real top-up removes the promotional flag and allows marketplace earnings."""
        from teardrop import agent_runtime

        graph_mock = MagicMock()
        graph_mock.ainvoke = AsyncMock(return_value={"messages": [], "task_status": "completed"})

        ctx = _RunContext(
            graph=graph_mock,
            org_lc_tools=[],
            org_tools_by_name={},
            mp_by_name={"acme/weather": object()},
            recalled=[],
            llm_config=None,
            org_name="test-org",
            credit_balance_usdc=500_000,
            is_promotional_credit=False,
            persisted_excluded_tools=[],
        )

        async def _noop_settlement(*args, **kwargs):
            kwargs["result"]["marketplace_stats_billable"] = True
            yield None

        with (
            patch.object(agent_runtime, "get_settings", return_value=test_settings),
            patch.object(agent_runtime, "_prepare_run_context", AsyncMock(return_value=ctx)),
            patch.object(agent_runtime, "fetch_usage_snapshot", AsyncMock(return_value=(None, {}))),
            patch.object(agent_runtime, "calculate_run_cost", AsyncMock(return_value=0)),
            patch.object(agent_runtime, "record_usage_event", AsyncMock()),
            patch.object(agent_runtime, "dispatch_settlement", _noop_settlement),
            patch.object(agent_runtime, "_record_marketplace_earnings", AsyncMock()) as earnings_mock,
            patch.object(test_settings, "marketplace_enabled", True),
        ):
            result = await run_agent_once(
                org_id="org-1",
                user_id="user-1",
                usage_user_id="user-1",
                usage_org_id="org-1",
                user_message="hello",
                run_id="run-1",
                thread_id="thread-1",
                billing=BillingResult(verified=True, billing_method="credit"),
                is_byok=False,
                org_llm_cfg=None,
                platform_fee=0,
                timeout_seconds=30,
            )

        initial_state = graph_mock.ainvoke.call_args.args[0]
        excluded = set(initial_state.metadata["_excluded_tool_names"])
        assert "acme/weather" not in excluded
        assert result.marketplace_stats_billable is True
        earnings_mock.assert_awaited_once()

    async def test_run_agent_once_records_shared_post_run_telemetry(self, test_settings):
        """Schedules, triggers, and A2A runs use the common runtime path."""
        from teardrop import agent_runtime

        graph_mock = MagicMock()
        graph_mock.ainvoke = AsyncMock(return_value={"messages": [], "task_status": "completed"})
        state_values = {"messages": ["final response"], "slots": {"quotes": {"ETH": "safe"}}}
        usage_data = {
            "tool_calls": 1,
            "tool_names": ["platform/weather"],
            "_tool_call_log": [{"tool_name": "platform/weather", "args_hash": "safe-hash"}],
        }
        ctx = _RunContext(
            graph=graph_mock,
            org_lc_tools=[],
            org_tools_by_name={},
            mp_by_name={},
            recalled=[],
            llm_config=None,
            org_name="test-org",
            credit_balance_usdc=None,
        )
        telemetry_mock = MagicMock()

        with (
            patch.object(agent_runtime, "get_settings", return_value=test_settings),
            patch.object(agent_runtime, "_prepare_run_context", AsyncMock(return_value=ctx)),
            patch.object(
                agent_runtime,
                "fetch_usage_snapshot",
                AsyncMock(return_value=(MagicMock(values=state_values), usage_data)),
            ),
            patch.object(agent_runtime, "calculate_run_cost", AsyncMock(return_value=0)),
            patch.object(agent_runtime, "record_usage_event", AsyncMock()),
            patch.object(agent_runtime, "record_post_run_telemetry", telemetry_mock),
        ):
            await run_agent_once(
                org_id="org-1",
                user_id="user-1",
                usage_user_id="user-1",
                usage_org_id="org-1",
                user_message="hello",
                run_id="run-1",
                thread_id="schedule-1",
                billing=BillingResult(verified=False),
                is_byok=False,
                org_llm_cfg=None,
                platform_fee=0,
                timeout_seconds=30,
                source="schedule",
            )

        telemetry_mock.assert_called_once_with(
            run_id="run-1",
            org_id="org-1",
            user_id="user-1",
            usage_data=usage_data,
            state_values=state_values,
            settings=test_settings,
            outcome=1,
            outcome_source="auto",
            thread_id="schedule-1",
            user_message="hello",
            source="schedule",
        )

    async def test_failure_usage_records_automated_failure_telemetry(self, test_settings):
        """Timeouts and executor failures contribute weak failure labels."""
        from teardrop import agent_runtime

        state_values = {"messages": ["partial response"]}
        usage_data = {"tool_calls": 1, "_tool_call_log": [{"tool_name": "platform/weather"}]}
        telemetry_mock = MagicMock()

        with (
            patch.object(
                agent_runtime,
                "fetch_usage_snapshot",
                AsyncMock(return_value=(MagicMock(values=state_values), usage_data)),
            ),
            patch.object(agent_runtime, "record_usage_event", AsyncMock()),
            patch.object(agent_runtime, "record_post_run_telemetry", telemetry_mock),
        ):
            usage_event, snapshot_values = await agent_runtime.record_failure_usage_event(
                graph=MagicMock(),
                config={},
                run_id="run-1",
                thread_id="thread-1",
                org_id="org-1",
                user_id="user-1",
                usage_user_id="user-1",
                usage_org_id="usage-org-1",
                duration_ms=10,
                llm_config=None,
                platform_fee=0,
                runtime_settings=test_settings,
                source="a2a",
            )

        telemetry_mock.assert_called_once_with(
            run_id="run-1",
            org_id="org-1",
            user_id="user-1",
            usage_data=usage_data,
            state_values=state_values,
            settings=test_settings,
            outcome=-1,
            outcome_source="auto",
            thread_id="thread-1",
            source="a2a",
        )
        assert snapshot_values == state_values

    async def test_run_agent_once_timeout_surfaces_partial_output(self, test_settings):
        """Run-level timeout emits the last assistant text, not a bare 'Task timed out.'"""
        from langchain_core.messages import AIMessage

        from teardrop import agent_runtime

        failure_usage_data = {"_tool_call_log": [{"tool_name": "record_predictions", "success": True}]}
        state_values = {
            "messages": [AIMessage(content="partial synthesis from tools")],
            "metadata": {"_usage": failure_usage_data},
        }
        ctx = _RunContext(
            graph=MagicMock(),
            org_lc_tools=[],
            org_tools_by_name={},
            mp_by_name={},
            recalled=[],
            llm_config=None,
            org_name="test-org",
            credit_balance_usdc=None,
        )

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        with (
            patch.object(agent_runtime, "get_settings", return_value=test_settings),
            patch.object(agent_runtime, "_prepare_run_context", AsyncMock(return_value=ctx)),
            patch.object(
                agent_runtime,
                "fetch_usage_snapshot",
                AsyncMock(return_value=(MagicMock(values=state_values), failure_usage_data)),
            ),
            patch.object(agent_runtime, "record_usage_event", AsyncMock()),
            patch.object(agent_runtime, "record_post_run_telemetry"),
        ):
            graph_mock = ctx.graph
            graph_mock.ainvoke = AsyncMock(side_effect=_hang)
            result = await agent_runtime.run_agent_once(
                org_id="org-1",
                user_id="user-1",
                usage_user_id="user-1",
                usage_org_id="org-1",
                user_message="hello",
                run_id="run-1",
                thread_id="thread-1",
                billing=BillingResult(verified=True, billing_method="credit"),
                is_byok=False,
                org_llm_cfg=None,
                platform_fee=0,
                timeout_seconds=0.05,
                source="schedule",
            )

        assert result.task_state == "timeout"
        assert result.output_text == "partial synthesis from tools"
        assert result.error == "Task timed out."
        assert result.usage_data == failure_usage_data

    async def test_run_agent_once_timeout_falls_back_when_no_partial(self, test_settings):
        """With no assistant text in state, timeout keeps the legacy message."""
        from langchain_core.messages import ToolMessage

        from teardrop import agent_runtime

        state_values = {
            "messages": [
                ToolMessage(content='{"secret":"must not become webhook text"}', tool_call_id="tool-1"),
            ],
            "metadata": {"_usage": "malformed"},
        }
        ctx = _RunContext(
            graph=MagicMock(),
            org_lc_tools=[],
            org_tools_by_name={},
            mp_by_name={},
            recalled=[],
            llm_config=None,
            org_name="test-org",
            credit_balance_usdc=None,
        )

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        with (
            patch.object(agent_runtime, "get_settings", return_value=test_settings),
            patch.object(agent_runtime, "_prepare_run_context", AsyncMock(return_value=ctx)),
            patch.object(agent_runtime, "fetch_usage_snapshot", AsyncMock(return_value=(MagicMock(values=state_values), {}))),
            patch.object(agent_runtime, "record_usage_event", AsyncMock()),
            patch.object(agent_runtime, "record_post_run_telemetry"),
        ):
            graph_mock = ctx.graph
            graph_mock.ainvoke = AsyncMock(side_effect=_hang)
            result = await agent_runtime.run_agent_once(
                org_id="org-1",
                user_id="user-1",
                usage_user_id="user-1",
                usage_org_id="org-1",
                user_message="hello",
                run_id="run-1",
                thread_id="thread-1",
                billing=BillingResult(verified=True, billing_method="credit"),
                is_byok=False,
                org_llm_cfg=None,
                platform_fee=0,
                timeout_seconds=0.05,
                source="schedule",
            )

        assert result.task_state == "timeout"
        assert result.output_text == "Task timed out."
