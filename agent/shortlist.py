# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Relevance-scored tool shortlisting for planner turns.

Binds only a keyword-scored subset of tool schemas to the planner when the org
has many tools, capping prompt tokens without changing which tools can run
(executor resolution stays global; unbound-but-real calls still execute).

Pure functions, no state: the same (request_text, tools, max_tools) triple always
derives the same set, so the selector is recomputed each planner turn and stays
consistent within a run (the latest human message is invariant during the tool
loop).
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "for",
        "to",
        "of",
        "in",
        "on",
        "my",
        "is",
        "are",
        "what",
        "how",
        "can",
        "you",
        "your",
        "show",
        "tell",
        "give",
        "please",
        "with",
        "from",
        "this",
        "that",
        "all",
        "any",
        "get",
    }
)
# Tools whose schema is bound regardless of request-token overlap. Intersected
# with the post-exclusion `tools` list at selection time, so an excluded tool
# (e.g. delegate_to_agent when a2a delegation is disabled) can never be revived.
ALWAYS_KEEP = frozenset(
    {
        "calculate",
        "get_datetime",
        "web_search",
        "resolve_ens",
        "delegate_to_agent",
        "http_fetch",
    }
)
SHORTLIST_MIN_TOOLS = len(ALWAYS_KEEP)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS}


def _name_desc(tool) -> tuple[str, str]:
    """Extract (name, description) from a dict or object tool (dict-or-attr idiom)."""
    if isinstance(tool, dict):
        return str(tool.get("name", "") or ""), str(tool.get("description", "") or "")
    return str(getattr(tool, "name", "") or ""), str(getattr(tool, "description", "") or "")


def tool_name(tool) -> str:
    """Return a tool's name whether it is a dict or an object."""
    return _name_desc(tool)[0]


def select_shortlisted_tools(request_text: str, tools: list, *, max_tools: int) -> list:
    """Return a relevance-scored subset of ``tools`` to bind for this planner turn.

    When the unique tool count does not exceed ``max_tools`` and names are
    unique, the input list is returned unchanged. Otherwise, always-keep tools
    survive without consuming a relevance slot and remaining slots use token
    overlap, with stable input order as the fallback when no overlap exists.
    Duplicate names are collapsed with the last entry winning, matching
    executor resolution semantics.
    """
    if max_tools < 0:
        raise ValueError("max_tools must be non-negative")

    by_name: dict[str, object] = {}
    for t in tools:
        by_name[_name_desc(t)[0]] = t  # later wins: org shadows platform

    always_names = ALWAYS_KEEP & by_name.keys()
    if len(always_names) > max_tools:
        raise ValueError(f"max_tools={max_tools} cannot retain {len(always_names)} always-keep tools")
    if len(by_name) == len(tools) and len(tools) <= max_tools:
        return tools
    req = _tokens(request_text)
    scored = sorted(  # ties keep insertion order (stable sort)
        ((len(req & _tokens(f"{name.replace('_', ' ')} {_name_desc(t)[1]}")), name) for name, t in by_name.items()),
        key=lambda sn: -sn[0],
    )
    candidates = [name for _score, name in scored if name not in always_names]
    hits = [name for score, name in scored if score >= 1 and name not in always_names]
    headroom = max_tools - len(always_names)
    selected_names = hits[:headroom]
    if not selected_names:
        selected_names = candidates[:headroom]
    keep = set(selected_names[:headroom]) | always_names

    selected: list = []
    emitted: set[str] = set()
    for tool in tools:
        name = tool_name(tool)
        if name in keep and name not in emitted:
            selected.append(by_name[name])
            emitted.add(name)
    return selected
