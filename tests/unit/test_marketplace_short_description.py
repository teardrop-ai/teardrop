"""Unit tests for the MarketplaceTool.short_description Pydantic field.

Verifies the field is optional (backward-compatible), defaults to empty
string, and can be round-tripped through model_dump().
"""

from __future__ import annotations

from marketplace import MarketplaceTool


def _make_tool(**overrides) -> MarketplaceTool:
    base = {
        "name": "get_balance",
        "qualified_name": "acme/get_balance",
        "description": "Returns the USDC balance for a given wallet address.",
        "marketplace_description": "Wallet USDC balance lookup — fast and indexed.",
        "input_schema": {"type": "object", "properties": {"address": {"type": "string"}}},
        "cost_usdc": 10_000,
        "author_org_name": "Acme",
        "author_org_slug": "acme",
    }
    base.update(overrides)
    return MarketplaceTool(**base)


def test_short_description_defaults_to_empty_string():
    """Backward compat: existing constructors that omit short_description still validate."""
    tool = _make_tool()
    assert tool.short_description == ""


def test_short_description_round_trips():
    tool = _make_tool(short_description="Wallet USDC balance lookup")
    assert tool.short_description == "Wallet USDC balance lookup"
    dumped = tool.model_dump()
    assert dumped["short_description"] == "Wallet USDC balance lookup"


def test_short_description_can_match_description():
    """The catalog constructor is allowed to set short_description == description."""
    desc = "Returns the USDC balance for a given wallet address."
    tool = _make_tool(description=desc, short_description=desc)
    assert tool.short_description == tool.description
