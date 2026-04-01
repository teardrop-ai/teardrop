"""Unit tests for tools/registry.py — ToolRegistry CRUD, versioning, and exports."""

from __future__ import annotations

from pydantic import BaseModel

import pytest

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
    reg.register(_make_tool())
    skills = reg.to_a2a_skills()
    assert len(skills) == 1
    skill = skills[0]
    assert skill["name"] == "test_tool"
    assert "description" in skill
    assert "tags" in skill
    assert "version" in skill


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
