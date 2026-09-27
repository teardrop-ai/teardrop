# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Compatibility smoke tests for file-level refactor re-exports."""

from __future__ import annotations


def test_agent_nodes_reexports_still_resolve():
    import agent.nodes as nodes

    assert callable(nodes._resolve_planner_llm)
    assert callable(nodes._build_cached_planner_prefix)
    assert callable(nodes._provider_api_key)


def test_llm_usage_reexport_still_resolves():
    from agent.llm import extract_usage

    assert callable(extract_usage)


def test_teardrop_app_surface_still_resolves():
    from teardrop.app import app, lifespan, require_admin

    assert app is not None
    assert callable(lifespan)
    assert require_admin is not None


def test_agent_route_symbols_keep_legacy_imports():
    from teardrop.app import AgentToolItem, list_agent_tools
    from teardrop.routers import agent as legacy_agent
    from teardrop.routers import agent_decisions, agent_tools

    tool_symbols = (
        "AgentToolItem",
        "ToolExclusionActionResponse",
        "ToolExclusionListResponse",
        "ToolExclusionRemovedResponse",
        "ToolExclusionRequest",
        "create_agent_tool_exclusion",
        "delete_agent_tool_exclusion",
        "get_agent_tool_exclusions",
        "list_agent_tools",
    )
    decision_symbols = (
        "AgentDecisionListResponse",
        "AgentDecisionRecord",
        "RunOutcomeRequest",
        "RunOutcomeResponse",
        "list_agent_decisions",
        "set_agent_run_outcome",
    )

    assert AgentToolItem is agent_tools.AgentToolItem
    assert list_agent_tools is agent_tools.list_agent_tools
    for module, symbols in ((agent_tools, tool_symbols), (agent_decisions, decision_symbols)):
        for symbol in symbols:
            assert getattr(legacy_agent, symbol) is getattr(module, symbol)


def test_marketplace_facade_price_lookup_still_resolves():
    from marketplace import get_platform_tool_price

    assert callable(get_platform_tool_price)
