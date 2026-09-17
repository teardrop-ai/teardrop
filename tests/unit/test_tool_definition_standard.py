# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Fail-closed standard for agent-consumer tool definitions.

Every registered tool must carry the agent-commerce contract: a decision-oriented
description plus use_when / limitations / alternatives guidance, with no assumptions
of human-only usage (env-var setup instructions, dashboard/UI workflows, etc.).
"""

from __future__ import annotations

import re

import pytest

from tools.definitions import register_all
from tools.registry import ToolRegistry

# Tools not yet backfilled to the standard. MUST be empty before merge; new tools
# may never be added here.
_EXEMPT: frozenset[str] = frozenset()

# Guidance fields bound a planner-context budget per tool.
_MIN_GUIDANCE_CHARS = 40
_MAX_GUIDANCE_CHARS = 500
_MAX_DESCRIPTION_CHARS = 700

# Human-usage assumptions: env-var setup strings ("Set TAVILY_API_KEY to activate",
# "Requires ETHEREUM_RPC_URL") belong in docs/configuration.md, not agent-facing text.
# Only the verb is case-insensitive; the env-var token must stay uppercase so
# snake_case parameter names like chain_id do not false-positive.
_ENV_SETUP_RE = re.compile(r"\b(?i:set|requires?|configure|export|provide)\b[^.]*?\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")
# Workflow words that presume a human operating a UI rather than an agent calling a tool.
_HUMAN_WORKFLOW_WORDS = ("dashboard", "settings page", "click ", "browser window")


def _fresh_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all(registry)
    return registry


def _latest_tools() -> list:
    registry = _fresh_registry()
    tools = registry.list_latest()
    assert tools, "register_all produced no tools"
    return tools


def test_every_tool_has_agent_commerce_guidance():
    for tool in _latest_tools():
        if tool.name in _EXEMPT:
            continue
        assert tool.use_when, f"{tool.name}: missing use_when"
        assert tool.limitations, f"{tool.name}: missing limitations"
        assert tool.alternatives, f"{tool.name}: missing alternatives"


def test_guidance_fields_within_length_bounds():
    for tool in _latest_tools():
        if tool.name in _EXEMPT:
            continue
        for field in ("use_when", "limitations"):
            value = getattr(tool, field)
            assert _MIN_GUIDANCE_CHARS <= len(value) <= _MAX_GUIDANCE_CHARS, (
                f"{tool.name}.{field}: length {len(value)} outside [{_MIN_GUIDANCE_CHARS}, {_MAX_GUIDANCE_CHARS}]"
            )
        assert len(tool.description) <= _MAX_DESCRIPTION_CHARS, (
            f"{tool.name}.description: length {len(tool.description)} exceeds {_MAX_DESCRIPTION_CHARS}"
        )


def test_alternatives_resolve_to_registered_tools_and_exclude_self():
    registry = _fresh_registry()
    known = {t.name for t in registry.list_latest(include_deprecated=True)}
    for tool in _latest_tools():
        if tool.name in _EXEMPT:
            continue
        for alt in tool.alternatives:
            assert alt != tool.name, f"{tool.name}: alternatives contains itself"
            assert alt in known, f"{tool.name}: alternative '{alt}' is not a registered tool"


def test_descriptions_free_of_human_usage_assumptions():
    for tool in _latest_tools():
        if tool.name in _EXEMPT:
            continue
        for field in ("description", "use_when", "limitations"):
            text = getattr(tool, field)
            match = _ENV_SETUP_RE.search(text)
            assert match is None, f"{tool.name}.{field}: env-var setup assumption: {match.group(0)!r}"
            for word in _HUMAN_WORKFLOW_WORDS:
                assert word not in text.lower(), f"{tool.name}.{field}: human-workflow wording {word!r}"


def test_every_tool_has_description_and_tags():
    for tool in _latest_tools():
        assert tool.description.strip(), f"{tool.name}: missing description"
        assert tool.tags, f"{tool.name}: missing tags"


@pytest.mark.parametrize("exempt_name", sorted(_EXEMPT))
def test_exempt_names_are_registered(exempt_name: str):
    registry = _fresh_registry()
    assert registry.get(exempt_name) is not None, f"_EXEMPT entry '{exempt_name}' is not a registered tool"
