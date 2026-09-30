# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for tools/capabilities.py — shared capability manifest and projections."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from tools import capabilities
from tools.capabilities import (
    build_capability_manifest,
    get_capability_manifest,
    set_capability_manifest,
    to_a2a_skill,
    to_a2a_tool,
    to_mcp_server_card_tool,
)
from tools.registry import ToolDefinition, ToolRegistry


class _In(BaseModel):
    value: int


class _Out(BaseModel):
    result: int


async def _noop(value: int) -> dict:
    return {"result": value}


@pytest.fixture
def reg(monkeypatch) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="guided_tool",
            version="1.0.0",
            description="A guided tool.",
            tags=["test"],
            examples=["Check this test input."],
            use_when="Use when the task needs guidance.",
            limitations="Only works on mainnet.",
            alternatives=["other_tool"],
            input_schema=_In,
            output_schema=_Out,
            implementation=_noop,
        )
    )
    monkeypatch.setattr(capabilities, "registry", registry)
    return registry


def _plain(name: str, version: str = "1.0.0") -> ToolDefinition:
    return ToolDefinition(name=name, version=version, description="Plain.", input_schema=_In, implementation=_noop)


def _community(name="acme/weather", tool_type="community"):
    return SimpleNamespace(
        qualified_name=name,
        tool_type=tool_type,
        marketplace_description="Weather lookup",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
        output_schema={"type": "object"},
        cost_usdc=5_000,
    )


def test_platform_card_projection_carries_guidance_schema_and_reputation(reg):
    reputation = {"platform/guided_tool": {"reputation_score": 0.9, "sample_size": 5}}

    (capability,) = build_capability_manifest(reputation)
    entry = to_mcp_server_card_tool(capability)

    assert capability.qualified_name == "platform/guided_tool"
    assert capability.x402_payable is True
    assert entry["name"] == "guided_tool"
    assert entry["title"] == "Guided Tool"
    assert entry["annotations"] == {"readOnlyHint": True}
    assert entry["inputSchema"] == _In.model_json_schema()
    assert entry["outputSchema"] == _Out.model_json_schema()
    assert entry["use_when"] == "Use when the task needs guidance."
    assert entry["limitations"] == "Only works on mainnet."
    assert entry["alternatives"] == ["other_tool"]
    assert entry["reputation"] == reputation["platform/guided_tool"]
    assert entry["_meta"] == {"teardrop/reputation": {"reputation_score": 0.9, "sample_size": 5}}


def test_unobserved_reputation_and_unknown_price_are_omitted(reg):
    reputation = {"platform/guided_tool": {"reputation_score": 0, "sample_size": 0, "success_rate": 0}}

    (capability,) = build_capability_manifest(reputation)
    entry = to_mcp_server_card_tool(capability)

    assert capability.reputation is None
    assert capability.cost_usdc is None
    assert "reputation" not in entry
    assert "_meta" not in entry


def test_known_price_is_projected_as_mcp_price_meta(reg):
    (capability,) = build_capability_manifest(prices={"guided_tool": 2_000})

    assert to_mcp_server_card_tool(capability)["_meta"] == {"teardrop/price": {"cost_usdc": 2_000, "unit": "call"}}


def test_community_rows_are_credit_only_and_platform_rows_are_skipped(reg):
    reputation = {"acme/weather": {"reputation_score": 0.8, "sample_size": 3}}

    manifest = build_capability_manifest(
        reputation, community=[_community(), _community("platform/guided_tool", tool_type="platform")]
    )

    assert [c.qualified_name for c in manifest] == ["platform/guided_tool", "acme/weather"]
    weather = manifest[1]
    assert weather.kind == "community"
    assert weather.name == "acme/weather"
    assert weather.x402_payable is False
    assert weather.cost_usdc == 5_000
    assert weather.output_schema == {"type": "object"}
    assert weather.reputation == {"reputation_score": 0.8, "sample_size": 3}


def test_x402_payable_matches_gateway_rule():
    from teardrop.mcp_gateway import _x402_payable

    manifest = build_capability_manifest(community=[_community()])

    assert manifest
    assert all(capability.x402_payable == _x402_payable(capability.name) for capability in manifest)


def test_snapshot_falls_back_to_registry_only_manifest(reg):
    assert [c.name for c in get_capability_manifest()] == ["guided_tool"]

    snapshot = build_capability_manifest(prices={"guided_tool": 1}, community=[_community()])
    set_capability_manifest(snapshot)

    assert get_capability_manifest() is snapshot


def test_a2a_projections_carry_card_fields_and_reputation(reg):
    reputation = {"platform/guided_tool": {"reputation_score": 0.9, "unique_caller_count": 5}}

    (capability,) = build_capability_manifest(reputation)
    skill = to_a2a_skill(capability)
    tool = to_a2a_tool(capability)

    assert skill == {
        "id": "guided_tool",
        "name": "guided_tool",
        "description": "A guided tool.",
        "tags": ["test"],
        "version": "1.0.0",
        "examples": ["Check this test input."],
        "use_when": "Use when the task needs guidance.",
        "limitations": "Only works on mainnet.",
        "alternatives": ["other_tool"],
        "reputation": reputation["platform/guided_tool"],
    }
    assert tool["input_schema"] == _In.model_json_schema()
    assert tool["output_schema"] == _Out.model_json_schema()
    assert tool["reputation"] == reputation["platform/guided_tool"]
    assert "examples" not in tool


def test_a2a_projections_omit_empty_optional_fields(reg):
    reg.register(_plain("plain_tool"))

    plain = next(c for c in build_capability_manifest({"platform/other": {"reputation_score": 1.0}}) if c.name == "plain_tool")
    skill = to_a2a_skill(plain)
    tool = to_a2a_tool(plain)

    for key in ("examples", "use_when", "limitations", "alternatives", "reputation", "deprecated"):
        assert key not in skill
    for key in ("output_schema", "use_when", "limitations", "alternatives", "reputation", "deprecated"):
        assert key not in tool


def test_deprecation_prefers_active_version_and_flags_fully_deprecated_tools(reg):
    reg.register(_plain("versioned", "1.0.0"))
    reg.register(_plain("versioned", "2.0.0"))
    reg.deprecate("versioned", "2.0.0")
    reg.register(_plain("retired", "1.0.0"))
    reg.deprecate("retired", "1.0.0", superseded_by="guided_tool")

    by_name = {c.name: c for c in build_capability_manifest(prices={"versioned": 1})}

    assert by_name["versioned"].version == "1.0.0"
    assert by_name["versioned"].deprecated is False
    retired = by_name["retired"]
    assert retired.deprecated is True
    assert retired.cost_usdc is None
    assert to_a2a_skill(retired)["deprecated"] is True
    assert to_a2a_skill(retired)["superseded_by"] == "guided_tool"
    assert to_a2a_tool(retired)["deprecated"] is True
