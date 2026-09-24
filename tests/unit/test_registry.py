# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for tools/registry.py — ToolRegistry CRUD, versioning, and exports."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tools.registry import ToolDefinition, ToolRegistry

# ── Helpers ───────────────────────────────────────────────────────────────────


class _In(BaseModel):
    value: int


class _Out(BaseModel):
    result: int


async def _noop(value: int) -> dict:
    return {"result": value}


def _make_tool(name: str = "test_tool", version: str = "1.0.0") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=version,
        description="A test tool.",
        tags=["test"],
        input_schema=_In,
        output_schema=_Out,
        implementation=_noop,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_register_and_get():
    reg = ToolRegistry()
    tool = _make_tool()
    reg.register(tool)
    found = reg.get("test_tool", "1.0.0")
    assert found is tool


def test_tool_definition_propagates_capture_args_metadata():
    tool = _make_tool()
    assert tool.to_langchain_tool().metadata["capture_args"] is False

    tool.capture_args = True
    assert tool.to_langchain_tool().metadata["capture_args"] is True


def test_get_latest_returns_highest_version():
    reg = ToolRegistry()
    reg.register(_make_tool(version="1.0.0"))
    reg.register(_make_tool(version="2.0.0"))
    reg.register(_make_tool(version="1.5.0"))
    latest = reg.get("test_tool")
    assert latest.version == "2.0.0"


def test_get_missing_returns_none():
    reg = ToolRegistry()
    assert reg.get("nonexistent") is None
    assert reg.get("nonexistent", "1.0.0") is None


def test_list_all_excludes_deprecated_by_default():
    reg = ToolRegistry()
    reg.register(_make_tool(version="1.0.0"))
    reg.register(_make_tool(version="2.0.0"))
    reg.deprecate("test_tool", "1.0.0")
    tools = reg.list_all()
    versions = {t.version for t in tools}
    assert "1.0.0" not in versions
    assert "2.0.0" in versions


def test_list_all_include_deprecated():
    reg = ToolRegistry()
    reg.register(_make_tool(version="1.0.0"))
    reg.deprecate("test_tool", "1.0.0")
    tools = reg.list_all(include_deprecated=True)
    assert len(tools) == 1
    assert tools[0].deprecated is True


def test_deprecate_missing_tool_raises():
    reg = ToolRegistry()
    with pytest.raises(KeyError):
        reg.deprecate("nonexistent", "1.0.0")


def test_deprecate_sets_superseded_by():
    reg = ToolRegistry()
    reg.register(_make_tool(version="1.0.0"))
    reg.register(_make_tool(version="2.0.0"))
    reg.deprecate("test_tool", "1.0.0", superseded_by="2.0.0")
    old = reg.get("test_tool", "1.0.0")
    assert old.superseded_by == "2.0.0"


def test_get_latest_skips_deprecated():
    reg = ToolRegistry()
    reg.register(_make_tool(version="1.0.0"))
    reg.register(_make_tool(version="2.0.0"))
    reg.deprecate("test_tool", "2.0.0")
    latest = reg.get("test_tool")
    assert latest.version == "1.0.0"


def test_get_latest_returns_none_when_all_deprecated():
    reg = ToolRegistry()
    reg.register(_make_tool(version="1.0.0"))
    reg.deprecate("test_tool", "1.0.0")
    assert reg.get("test_tool") is None


def test_to_langchain_tools_returns_list():
    reg = ToolRegistry()
    reg.register(_make_tool())
    lc_tools = reg.to_langchain_tools()
    assert len(lc_tools) == 1
    assert lc_tools[0].name == "test_tool"


def test_to_a2a_skills_shape():
    reg = ToolRegistry()
    tool = _make_tool()
    tool.examples = ["Check this test input."]
    reg.register(tool)
    skills = reg.to_a2a_skills()
    assert len(skills) == 1
    skill = skills[0]
    assert skill["name"] == "test_tool"
    assert "description" in skill
    assert "tags" in skill
    assert "version" in skill
    assert skill["examples"] == ["Check this test input."]
    assert "reputation" not in skill


def test_to_a2a_skills_omits_empty_examples():
    reg = ToolRegistry()
    reg.register(_make_tool())

    assert "examples" not in reg.to_a2a_skills()[0]


def test_public_exports_include_reputation_when_supplied():
    reg = ToolRegistry()
    reg.register(_make_tool())
    reputation = {"platform/test_tool": {"reputation_score": 0.9, "unique_caller_count": 5}}

    skill = reg.to_a2a_skills(reputation)[0]
    tool = reg.to_a2a_tool_list(reputation)[0]
    mcp_tool = reg.to_mcp_server_card_tools(reputation)[0]

    assert skill["reputation"] == reputation["platform/test_tool"]
    assert tool["reputation"] == reputation["platform/test_tool"]
    assert mcp_tool["reputation"] == reputation["platform/test_tool"]


def test_public_cards_do_not_rate_tools_with_no_observations():
    reg = ToolRegistry()
    reg.register(_make_tool())
    reputation = {"platform/test_tool": {"reputation_score": 0, "sample_size": 0, "success_rate": 0}}

    assert "reputation" not in reg.to_a2a_skills(reputation)[0]
    assert "reputation" not in reg.to_a2a_tool_list(reputation)[0]
    assert "reputation" not in reg.to_mcp_server_card_tools(reputation)[0]


def test_dynamic_mcp_defs_include_reputation_in_description():
    reg = ToolRegistry()
    reg.register(_make_tool())
    reputation = {
        "platform/test_tool": {
            "reputation_score": 0.9,
            "success_rate": 0.98,
            "sample_size": 150,
            "average_latency_ms": 210,
        }
    }

    definition = reg.to_mcp_tool_defs(reputation)[0]

    assert "Observed quality: score=0.90, success=98.0%, sample_size=150, latency=210ms." in definition["description"]
    assert reg.to_mcp_tool_defs()[0]["description"] == _make_tool().description


def test_dynamic_mcp_defs_include_structured_reputation_meta():
    reg = ToolRegistry()
    reg.register(_make_tool())
    reputation = {
        "platform/test_tool": {
            "reputation_score": 0.9,
            "success_rate": 0.98,
            "sample_size": 150,
            "confidence": 0.71,
            "freshness": 1.0,
            "average_latency_ms": 210,
            "unique_caller_count": 7,
        }
    }

    definition = reg.to_mcp_tool_defs(reputation)[0]

    assert definition["meta"] == {
        "teardrop/reputation": {
            "reputation_score": 0.9,
            "success_rate": 0.98,
            "sample_size": 150,
            "confidence": 0.71,
            "freshness": 1.0,
            "average_latency_ms": 210,
            "unique_caller_count": 7,
        }
    }


def test_dynamic_mcp_defs_omit_meta_without_reputation():
    reg = ToolRegistry()
    reg.register(_make_tool())

    assert reg.to_mcp_tool_defs()[0]["meta"] is None
    assert reg.to_mcp_tool_defs({"platform/other": {"reputation_score": 1.0}})[0]["meta"] is None


def test_dynamic_mcp_defs_meta_ignores_non_numeric_fields():
    reg = ToolRegistry()
    reg.register(_make_tool())
    reputation = {
        "platform/test_tool": {
            "reputation_score": "not-a-number",
            "success_rate": None,
            "sample_size": "invalid",
            "average_latency_ms": {},
        }
    }

    assert reg.to_mcp_tool_defs(reputation)[0]["meta"] is None


def test_dynamic_mcp_defs_suppress_all_zero_reputation():
    """Unrated tools (all-zero COALESCE rows) must not advertise score=0.00."""
    reg = ToolRegistry()
    reg.register(_make_tool())
    reputation = {
        "platform/test_tool": {
            "reputation_score": 0.0,
            "success_rate": 0.0,
            "sample_size": 0.0,
            "confidence": 0.0,
            "freshness": 0.0,
            "average_latency_ms": 0.0,
        }
    }

    definition = reg.to_mcp_tool_defs(reputation)[0]

    assert definition["meta"] is None
    assert "Observed quality:" not in definition["description"]


def test_dynamic_mcp_defs_keep_fractional_samples_and_private_caller_counts():
    reg = ToolRegistry()
    reg.register(_make_tool())
    definition = reg.to_mcp_tool_defs(
        {"platform/test_tool": {"reputation_score": 0.8, "sample_size": 0.5, "unique_caller_count": 1}}
    )[0]

    assert "sample_size=0.5" in definition["description"]
    assert definition["meta"] == {"teardrop/reputation": {"reputation_score": 0.8, "sample_size": 0.5}}


def test_dynamic_mcp_defs_reject_nonfinite_metrics():
    reg = ToolRegistry()
    reg.register(_make_tool())
    definition = reg.to_mcp_tool_defs({"platform/test_tool": {"reputation_score": float("nan"), "sample_size": float("inf")}})[0]

    assert definition["meta"] is None
    assert "Observed quality:" not in definition["description"]


def test_dynamic_mcp_defs_ignore_malformed_reputation():
    reg = ToolRegistry()
    reg.register(_make_tool())
    reputation = {
        "platform/test_tool": {
            "reputation_score": "not-a-number",
            "success_rate": None,
            "sample_size": "invalid",
            "average_latency_ms": {},
        }
    }

    assert reg.to_mcp_tool_defs(reputation)[0]["description"] == _make_tool().description


def test_dynamic_mcp_defs_include_guidance_in_description():
    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="guided_tool",
            version="1.0.0",
            description="A guided tool.",
            tags=["test"],
            use_when="Use when the task needs guidance.",
            limitations="Only works on mainnet.",
            alternatives=["other_tool", "another_tool"],
            input_schema=_In,
            output_schema=_Out,
            implementation=_noop,
        )
    )

    description = reg.to_mcp_tool_defs()[0]["description"]
    assert description.startswith("A guided tool.")
    assert "Use when: Use when the task needs guidance." in description
    assert "Limitations: Only works on mainnet." in description
    assert "Alternatives: other_tool, another_tool" in description
    assert reg.to_mcp_tool_defs(reputation=None)[0]["description"] == description


def test_dynamic_mcp_defs_rebuild_without_guidance_duplication():
    reg = ToolRegistry()
    reg.register(_make_tool(name="guided_tool", version="1.0.0"))
    reg.register(
        ToolDefinition(
            name="guided_tool",
            version="1.1.0",
            description="A guided tool.",
            tags=["test"],
            use_when="Use when the task needs guidance.",
            limitations="Only works on mainnet.",
            alternatives=["other_tool"],
            input_schema=_In,
            output_schema=_Out,
            implementation=_noop,
        )
    )

    first = reg.to_mcp_tool_defs()[0]["description"]
    # Re-deriving after a reputation-style refresh must not stack guidance sections.
    second = reg.to_mcp_tool_defs()[0]["description"]
    assert second == first
    assert second.count("Use when:") == 1


def test_public_exports_ignore_unknown_reputation():
    reg = ToolRegistry()
    reg.register(_make_tool())

    assert "reputation" not in reg.to_a2a_skills({"platform/other": {"reputation_score": 1.0}})[0]


def test_agent_commerce_fields_emitted_when_present_and_omitted_when_empty():
    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="guided_tool",
            version="1.0.0",
            description="A guided tool.",
            tags=["test"],
            use_when="Use when the task needs guidance.",
            limitations="Only works on mainnet.",
            alternatives=["other_tool"],
            input_schema=_In,
            output_schema=_Out,
            implementation=_noop,
        )
    )
    reg.register(_make_tool(name="plain_tool"))

    skill = reg.to_a2a_skills()[0]
    assert skill["use_when"] == "Use when the task needs guidance."
    assert skill["limitations"] == "Only works on mainnet."
    assert skill["alternatives"] == ["other_tool"]

    tool = reg.to_a2a_tool_list()[0]
    assert tool["use_when"] == "Use when the task needs guidance."
    assert tool["limitations"] == "Only works on mainnet."
    assert tool["alternatives"] == ["other_tool"]

    mcp_tool = reg.to_mcp_server_card_tools()[0]
    assert mcp_tool["use_when"] == "Use when the task needs guidance."
    assert mcp_tool["limitations"] == "Only works on mainnet."
    assert mcp_tool["alternatives"] == ["other_tool"]

    # Empty fields are omitted entirely (backward compatible).
    plain_skill = reg.to_a2a_skills()[1]
    assert "use_when" not in plain_skill
    assert "limitations" not in plain_skill
    assert "alternatives" not in plain_skill


def test_show_on_agent_card_defaults_true():
    tool = _make_tool()
    assert tool.show_on_agent_card is True


def test_to_a2a_skills_excludes_hidden_tools():
    reg = ToolRegistry()
    reg.register(_make_tool(name="visible_tool"))
    hidden = _make_tool(name="hidden_tool")
    hidden.show_on_agent_card = False
    reg.register(hidden)

    skills = reg.to_a2a_skills()
    names = {s["name"] for s in skills}
    assert "visible_tool" in names
    assert "hidden_tool" not in names


def test_to_a2a_tool_list_excludes_hidden_tools():
    reg = ToolRegistry()
    reg.register(_make_tool(name="visible_tool"))
    hidden = _make_tool(name="hidden_tool")
    hidden.show_on_agent_card = False
    reg.register(hidden)

    tools = reg.to_a2a_tool_list()
    names = {t["name"] for t in tools}
    assert "visible_tool" in names
    assert "hidden_tool" not in names


def test_hidden_tool_still_available_via_langchain_and_mcp():
    """show_on_agent_card only trims the public A2A card — the tool must
    remain fully callable via LangChain binding and MCP export."""
    reg = ToolRegistry()
    hidden = _make_tool(name="hidden_tool")
    hidden.show_on_agent_card = False
    reg.register(hidden)

    lc_names = {t.name for t in reg.to_langchain_tools()}
    mcp_names = {t["name"] for t in reg.to_mcp_tool_defs()}
    assert "hidden_tool" in lc_names
    assert "hidden_tool" in mcp_names


def test_duplicate_registration_overwrites_with_warning(caplog):
    import logging

    reg = ToolRegistry()
    reg.register(_make_tool())
    with caplog.at_level(logging.WARNING, logger="tools.registry"):
        reg.register(_make_tool())  # same name+version
    assert any("Overwriting" in r.message for r in caplog.records)


def test_list_latest_one_per_name():
    reg = ToolRegistry()
    reg.register(_make_tool(name="alpha", version="1.0.0"))
    reg.register(_make_tool(name="alpha", version="2.0.0"))
    reg.register(_make_tool(name="beta", version="1.0.0"))
    latest = reg.list_latest()
    names = [t.name for t in latest]
    assert names.count("alpha") == 1
    assert "beta" in names
    alpha = next(t for t in latest if t.name == "alpha")
    assert alpha.version == "2.0.0"


def test_assess_counterparty_risk_registered():
    from tools.definitions import register_all

    reg = ToolRegistry()
    register_all(reg)
    tool = reg.get("assess_counterparty_risk")
    assert tool is not None
    assert tool.name == "assess_counterparty_risk"
    assert tool.version == "1.0.0"
    assert tool.use_when != ""
    assert "get_wallet_approvals" in tool.alternatives


def test_validate_opportunity_registered():
    from tools.definitions import register_all

    reg = ToolRegistry()
    register_all(reg)
    tool = reg.get("validate_opportunity")
    assert tool is not None
    assert tool.name == "validate_opportunity"
    assert tool.version == "1.0.0"
    assert tool.use_when != ""
    assert "get_yield_rates" in tool.alternatives


def test_every_implementation_accepts_input_schema_fields_as_kwargs():
    """Executor and LangChain paths both call implementations with schema fields as kwargs."""
    import inspect

    from tools.definitions import register_all

    reg = ToolRegistry()
    register_all(reg)

    failures: list[str] = []
    for tool in reg.list_all():
        sig = inspect.signature(tool.implementation)
        accepts_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if accepts_var_kw:
            continue
        bindable = {
            name
            for name, p in sig.parameters.items()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        missing = set(tool.input_schema.model_fields) - bindable
        if missing:
            failures.append(f"{tool.name}: {sorted(missing)}")

    assert not failures, f"Implementations cannot be called with their schema fields: {failures}"
