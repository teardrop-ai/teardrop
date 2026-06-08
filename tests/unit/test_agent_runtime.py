"""Unit tests for teardrop/agent_runtime.py — pre-run context assembly."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from teardrop.agent_runtime import _RunContext


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
