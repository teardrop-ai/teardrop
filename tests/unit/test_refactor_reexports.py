# SPDX-License-Identifier: BUSL-1.1
"""Compatibility smoke tests for file-level refactor re-exports."""

from __future__ import annotations


def test_agent_nodes_reexports_still_resolve():
    import agent.nodes as nodes

    assert callable(nodes._resolve_planner_llm)
    assert callable(nodes._build_cached_planner_prefix)
    assert callable(nodes._provider_api_key)


def test_teardrop_app_surface_still_resolves():
    from teardrop.app import app, lifespan, require_admin

    assert app is not None
    assert callable(lifespan)
    assert require_admin is not None


def test_marketplace_facade_price_lookup_still_resolves():
    from marketplace import get_platform_tool_price

    assert callable(get_platform_tool_price)
