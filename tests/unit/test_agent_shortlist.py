# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for agent/shortlist.py — the relevance-scored tool shortlist.

Pure function tests; no LLM calls or tool execution.
"""

from __future__ import annotations

import pytest

from agent.shortlist import select_shortlisted_tools


class _Tool:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description


def _yield_rates() -> _Tool:
    return _Tool("get_yield_rates", "Lending and borrowing yield rates for DeFi protocols")


def _wallet() -> _Tool:
    return _Tool("get_wallet_portfolio", "Tokens held in the user's wallet")


def _calculate() -> _Tool:
    return _Tool("calculate", "Evaluate a mathematical expression")


def test_overlap_selection_keeps_matching_drops_unmatched():
    tools = [_yield_rates(), _wallet(), _calculate()]
    selected = select_shortlisted_tools("compare aave usdc yield", tools, max_tools=2)
    names = [t.name for t in selected]
    assert "get_yield_rates" in names
    assert "get_wallet_portfolio" not in names
    # ALWAYS_KEEP schemas survive even when not token-matched.
    assert "calculate" in names


def test_cap_limits_result_and_preserves_stable_order():
    tools = [
        _Tool("tool_alpha", "alpha"),
        _Tool("tool_beta", "beta"),
        _Tool("tool_delta", "delta"),
    ]
    # All three score on their name; max_tools=2 keeps only the first two,
    # in original (stable) order among equal scores.
    selected = select_shortlisted_tools("alpha beta delta", tools, max_tools=2)
    names = [t.name for t in selected]
    assert names == ["tool_alpha", "tool_beta"]


def test_always_keep_match_does_not_displace_relevant_tool():
    tools = [_yield_rates(), _calculate(), _wallet()]
    selected = select_shortlisted_tools("calculate aave yield", tools, max_tools=2)
    names = [t.name for t in selected]
    assert names == ["get_yield_rates", "calculate"]


def test_always_keep_tools_fit_within_valid_cap():
    tools = [
        _yield_rates(),
        _calculate(),
        _Tool("web_search", "Search the web for current information"),
    ]
    selected = select_shortlisted_tools("compare yield aave", tools, max_tools=6)
    names = [t.name for t in selected]
    assert "calculate" in names
    assert "web_search" in names
    assert "get_yield_rates" in names
    assert len(names) <= 6


def test_max_tools_rejects_negative_value():
    with pytest.raises(ValueError, match="non-negative"):
        select_shortlisted_tools("compare yield", [_calculate()], max_tools=-1)


def test_max_tools_rejects_cap_smaller_than_present_always_keep_tools():
    tools = [_calculate(), _Tool("web_search", "Search the web")]
    with pytest.raises(ValueError, match="always-keep"):
        select_shortlisted_tools("compare yield", tools, max_tools=1)


def test_always_keep_absent_when_tool_excluded():
    # ALWAYS_KEEP is intersected with the provided tools, so a missing always-keep
    # tool is never resurrected (e.g. delegate_to_agent when a2a is disabled).
    tools = [_wallet()]
    selected = select_shortlisted_tools("compare aave usdc yield", tools, max_tools=12)
    assert all(t.name not in {"calculate", "delegate_to_agent"} for t in selected)


def test_zero_overlap_returns_input_unchanged():
    tools = [_yield_rates(), _wallet()]
    result = select_shortlisted_tools("unrelated query tokens", tools, max_tools=4)
    assert result is tools


def test_zero_overlap_is_capped_with_stable_fallback():
    tools = [_calculate(), _wallet(), _yield_rates()]
    selected = select_shortlisted_tools("unrelated query tokens", tools, max_tools=2)
    assert [tool.name for tool in selected] == ["calculate", "get_wallet_portfolio"]


def test_empty_request_text_is_capped_with_stable_fallback():
    tools = [_calculate(), _wallet(), _yield_rates()]
    selected = select_shortlisted_tools("   ", tools, max_tools=2)
    assert [tool.name for tool in selected] == ["calculate", "get_wallet_portfolio"]


def test_at_or_below_cap_returns_input_unchanged():
    tools = [_yield_rates(), _wallet()]
    result = select_shortlisted_tools("compare aave usdc yield", tools, max_tools=2)
    assert result is tools


def test_dict_org_tools_and_object_platform_tools_mixed():
    org = [{"name": "org_aave_yield", "description": "Lending and borrowing yield rates for DeFi protocols"}]
    platform = [_wallet(), _calculate()]
    selected = select_shortlisted_tools("compare aave usdc yield", platform + org, max_tools=2)
    names = [t["name"] if isinstance(t, dict) else t.name for t in selected]
    assert "org_aave_yield" in names
    assert "get_wallet_portfolio" not in names
    assert "calculate" in names


def test_name_collision_uses_later_entry():
    # Org entries shadow platform entries in executor resolution, so the
    # selector must bind only the later entry when names collide.
    platform = [{"name": "get_yield_rates", "description": "Platform yield rates"}]
    org = [{"name": "get_yield_rates", "description": "Org policy config for yield"}]
    tools = platform + org
    selected = select_shortlisted_tools("compare yield aave", tools, max_tools=4)
    assert selected == org


def test_settings_reject_shortlist_cap_below_always_keep_count():
    from teardrop.config import Settings

    with pytest.raises(ValueError, match="greater than or equal to 7"):
        Settings(agent_tool_shortlist_max_tools=5)
