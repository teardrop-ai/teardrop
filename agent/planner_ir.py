# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Structured planner IR for staged tool execution.

The planner may optionally emit a <plan>{...}</plan> JSON payload. This module
parses and validates that payload, and provides lightweight argument reference
resolution for dependent stages.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

_PLAN_TAG_RE = re.compile(r"<plan>\s*(\{.*?\})\s*</plan>", re.DOTALL | re.IGNORECASE)
_MAX_PLAN_BLOCK_CHARS = 4096
_REF_RE = re.compile(r"^\{\{\s*([a-zA-Z0-9_\-]+)(?:\.([a-zA-Z0-9_\-\.\[\]]+))?\s*\}\}$")


class PlanCall(BaseModel):
    """One tool call in a staged execution plan."""

    call_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class PlanStage(BaseModel):
    """A stage of calls that can run in parallel."""

    stage_id: int
    calls: list[PlanCall] = Field(default_factory=list)


class Plan(BaseModel):
    """Plan state persisted in AgentState across turns."""

    stages: list[PlanStage] = Field(default_factory=list)
    synthesizer_after_stage: int | None = None
    current_stage_index: int = 0
    completed_call_ids: list[str] = Field(default_factory=list)

    def is_done(self) -> bool:
        return self.current_stage_index >= len(self.stages)


def _lookup_path(root: Any, path: str | None) -> Any:
    if path is None or path == "":
        return root
    cur = root
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(part)
            cur = cur[part]
            continue
        if isinstance(cur, list):
            idx = int(part)
            cur = cur[idx]
            continue
        raise KeyError(part)
    return cur


def resolve_plan_references(value: Any, outputs_by_call_id: dict[str, Any]) -> Any:
    """Resolve '{{call_id.path}}' references inside call args."""
    if isinstance(value, dict):
        return {k: resolve_plan_references(v, outputs_by_call_id) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_plan_references(v, outputs_by_call_id) for v in value]
    if isinstance(value, str):
        match = _REF_RE.match(value.strip())
        if not match:
            return value
        call_id = match.group(1)
        path = match.group(2)
        base = outputs_by_call_id.get(call_id)
        if base is None:
            return value
        try:
            return _lookup_path(base, path)
        except Exception:
            return value
    return value


def validate_plan_dag(plan: Plan) -> None:
    """Validate call ids, tool names, and dependency graph consistency."""
    call_ids: set[str] = set()
    all_calls: list[PlanCall] = []
    for stage in plan.stages:
        for call in stage.calls:
            if not call.call_id:
                raise ValueError("Plan call_id must be non-empty")
            if not call.tool:
                raise ValueError(f"Plan call '{call.call_id}' is missing tool name")
            if call.call_id in call_ids:
                raise ValueError(f"Duplicate plan call_id '{call.call_id}'")
            call_ids.add(call.call_id)
            all_calls.append(call)

    graph: dict[str, list[str]] = {call.call_id: list(call.depends_on) for call in all_calls}
    for call in all_calls:
        for dep in call.depends_on:
            if dep not in call_ids:
                raise ValueError(f"Plan call '{call.call_id}' depends on unknown call_id '{dep}'")

    visiting: set[str] = set()
    visited: set[str] = set()

    def _dfs(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError("Plan contains a dependency cycle")
        visiting.add(node)
        for dep in graph.get(node, []):
            _dfs(dep)
        visiting.remove(node)
        visited.add(node)

    for call_id in graph:
        _dfs(call_id)


def parse_plan_from_text(text: str) -> Plan | None:
    """Extract and validate a <plan> JSON block from planner output text."""
    match = _PLAN_TAG_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1).strip()
    if len(raw) > _MAX_PLAN_BLOCK_CHARS:
        raise ValueError("Plan block exceeds maximum allowed size")
    payload = json.loads(raw)
    plan = Plan.model_validate(payload)
    validate_plan_dag(plan)
    return plan
