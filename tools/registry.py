# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Versioned tool registry for Teardrop.

Provides ToolDefinition (the canonical way to declare a tool) and
ToolRegistry (the singleton that holds all registered tools and
exports them as LangChain tools, A2A skills, and MCP definitions).

Owner map:
  - reputation helpers:  build_reputation_meta, format_mcp_quality_description (MCP ``_meta`` + description trailer)
  - ToolDefinition:      tool declaration model + LangChain conversion
  - ToolRegistry:        register/deprecate/get, exporters (to_langchain_tools, to_a2a_skills,
                         to_a2a_tool_list, to_mcp_server_card_tools, to_mcp_tool_defs)
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from langchain_core.tools import StructuredTool
from packaging.version import Version
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _mcp_safe_output_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return an MCP-safe output schema for live MCPServer registration.

    MCPServer only accepts object-root output schemas. For non-object roots we
    leave the live schema unset rather than changing the structured output shape
    seen by MCP clients.
    """
    if schema is None:
        return None
    if schema.get("type") == "object":
        return schema
    return None


# Namespaced `_meta` key for structured reputation. Follows the MCP `_meta`
# key grammar (vendor-prefix/name), matching the x402 SDK's `x402/payment`.
MCP_REPUTATION_META_KEY = "teardrop/reputation"

# Numeric reputation fields surfaced to programmatic MCP clients. Kept in sync
# with `marketplace.reputation._load_public_reputation` output keys.
_REPUTATION_META_FIELDS = (
    "reputation_score",
    "success_rate",
    "sample_size",
    "confidence",
    "freshness",
    "average_latency_ms",
    "unique_caller_count",
)


def _finite_reputation_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _has_reputation_signal(metrics: dict[str, Any] | None) -> bool:
    """Whether metrics represent observed calls rather than an unrated tool.

    ``marketplace.reputation`` emits all-zero rows for tools with no call
    history (LEFT JOIN + COALESCE). Advertising ``score=0.00`` for a brand-new
    tool would wrongly discourage selection, so both the description trailer and
    the structured ``_meta`` block are suppressed until real signal exists.
    """
    if not metrics:
        return False
    for field in ("reputation_score", "sample_size", "reputation_sample_size"):
        value = metrics.get(field)
        if _finite_reputation_number(value) and value > 0:
            return True
    return False


def build_reputation_meta(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the structured ``_meta`` block for a tool's reputation metrics.

    Returns ``None`` when the tool is unrated or has no usable numeric metrics,
    so callers omit the key entirely (MCP clients treat absent ``_meta`` as
    "unrated" rather than zero).
    """
    if not _has_reputation_signal(metrics):
        return None
    structured: dict[str, Any] = {}
    for field in _REPUTATION_META_FIELDS:
        value = metrics.get(field)
        if not _finite_reputation_number(value) or value < 0:
            continue
        if field == "unique_caller_count" and (not isinstance(value, int) or value < 5):
            continue
        structured[field] = value
    if not structured:
        return None
    return {MCP_REPUTATION_META_KEY: structured}


def format_mcp_quality_description(description: str, metrics: dict[str, Any] | None) -> str:
    if not _has_reputation_signal(metrics):
        return description
    metrics = metrics or {}
    quality_metrics: list[str] = []
    score = metrics.get("reputation_score")
    if _finite_reputation_number(score) and score >= 0:
        quality_metrics.append(f"score={score:.2f}")
    success_rate = metrics.get("success_rate")
    if _finite_reputation_number(success_rate) and success_rate >= 0:
        quality_metrics.append(f"success={success_rate:.1%}")
    sample_size = metrics.get("reputation_sample_size", metrics.get("sample_size"))
    if _finite_reputation_number(sample_size) and sample_size > 0:
        quality_metrics.append(f"sample_size={sample_size:g}")
    latency = metrics.get("average_latency_ms")
    if _finite_reputation_number(latency) and latency >= 0:
        quality_metrics.append(f"latency={latency:.0f}ms")
    if quality_metrics:
        return f"{description}\n\nObserved quality: {', '.join(quality_metrics)}."
    return description


class ToolDefinition(BaseModel):
    """Canonical description of a single versioned tool."""

    name: str = Field(..., description="Unique tool identifier, e.g. 'web_search'")
    version: str = Field(..., description="Semver string, e.g. '1.0.0'")
    description: str = Field(..., description="Human/agent-readable description")
    tags: list[str] = Field(default_factory=list, description="Categorisation tags")
    examples: list[str] = Field(
        default_factory=list,
        description="Example prompts for A2A skill discovery.",
    )
    use_when: str = Field(
        default="",
        description="Agent-commerce guidance: when an agent should choose this tool.",
    )
    limitations: str = Field(
        default="",
        description="Agent-commerce guidance: known constraints, exclusions, or caveats.",
    )
    alternatives: list[str] = Field(
        default_factory=list,
        description="Agent-commerce guidance: related tool names an agent may consider instead.",
    )
    input_schema: Any = Field(..., description="Pydantic BaseModel class for input validation")
    output_schema: Any = Field(default=None, description="Optional Pydantic BaseModel class for output validation")
    annotations: dict[str, Any] | None = Field(default=None, description="Optional MCP tool annotations (readOnlyHint, etc.)")
    timeout_seconds: float | None = Field(default=None, description="Optional per-call timeout override")
    max_calls_per_run: int | None = Field(default=None, description="Optional per-run call cap")
    capture_args: bool = Field(
        default=False,
        description="Whether the executor may retain validated JSON arguments for this tool.",
    )
    show_on_agent_card: bool = Field(
        default=True,
        description=(
            "Whether this tool is advertised in the public A2A agent-card "
            "(skills/tools sections). Commoditized utility/low-level RPC "
            "primitives are set False to keep the card focused on Teardrop's "
            "differentiated capabilities; the tool remains fully callable via "
            "MCP (to_mcp_server_card_tools) and GET /agent/tools regardless."
        ),
    )
    implementation: Callable[..., Any] = Field(..., description="Async callable that executes the tool")

    # Deprecation lifecycle
    deprecated: bool = False
    deprecated_at: datetime | None = None
    deprecation_days: int = 90
    superseded_by: str | None = Field(default=None, description="Version string of the replacement, e.g. '2.0.0'")

    model_config = {"arbitrary_types_allowed": True}

    # Helpers ──────────────────────────────────────────────────────────────────

    @property
    def parsed_version(self) -> Version:
        return Version(self.version)

    def to_langchain_tool(self) -> StructuredTool:
        """Convert this definition into a LangChain StructuredTool."""
        return StructuredTool.from_function(
            coroutine=self.implementation,
            name=self.name,
            description=self.description,
            args_schema=self.input_schema,
            metadata={
                "timeout_seconds": self.timeout_seconds,
                "output_schema": self.output_schema,
                "max_calls_per_run": self.max_calls_per_run,
                "capture_args": self.capture_args,
            },
        )


class ToolRegistry:
    """Thread-safe, versioned registry of all Teardrop tools.

    Internal storage: ``{name: {version_str: ToolDefinition}}``
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, ToolDefinition]] = defaultdict(dict)

    # ── Mutation ──────────────────────────────────────────────────────────────

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool definition. Overwrites if same name+version exists."""
        bucket = self._tools[tool.name]
        if tool.version in bucket:
            logger.warning("Overwriting tool %s v%s", tool.name, tool.version)
        bucket[tool.version] = tool
        logger.debug("Registered tool %s v%s", tool.name, tool.version)

    def deprecate(
        self,
        name: str,
        version: str,
        superseded_by: str | None = None,
    ) -> None:
        """Mark a specific tool version as deprecated."""
        tool = self.get(name, version)
        if tool is None:
            raise KeyError(f"Tool {name} v{version} not found in registry")
        tool.deprecated = True
        tool.deprecated_at = datetime.now(tz=timezone.utc)
        tool.superseded_by = superseded_by
        logger.info(
            "Deprecated tool %s v%s (superseded_by=%s)",
            name,
            version,
            superseded_by,
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    def get(self, name: str, version: str = "latest") -> ToolDefinition | None:
        """Return a specific tool version, or the latest non-deprecated version."""
        bucket = self._tools.get(name)
        if not bucket:
            return None
        if version != "latest":
            return bucket.get(version)
        return self._get_latest(bucket)

    def list_all(self, *, include_deprecated: bool = False) -> list[ToolDefinition]:
        """Return all registered tools, optionally including deprecated ones."""
        results: list[ToolDefinition] = []
        for bucket in self._tools.values():
            for tool in bucket.values():
                if include_deprecated or not tool.deprecated:
                    results.append(tool)
        return results

    def list_latest(self, *, include_deprecated: bool = False) -> list[ToolDefinition]:
        """Return only the latest version of each tool name."""
        results: list[ToolDefinition] = []
        for bucket in self._tools.values():
            tool = self._get_latest(bucket, include_deprecated=include_deprecated)
            if tool is not None:
                results.append(tool)
        return results

    # ── Export: LangChain ─────────────────────────────────────────────────────

    def to_langchain_tools(self) -> list[StructuredTool]:
        """Convert latest active tools to LangChain StructuredTool list."""
        return [t.to_langchain_tool() for t in self.list_latest()]

    def get_langchain_tools_by_name(self) -> dict[str, StructuredTool]:
        """Return a ``{name: StructuredTool}`` mapping for the tool executor."""
        return {t.name: t.to_langchain_tool() for t in self.list_latest()}

    # ── Export: A2A ───────────────────────────────────────────────────────────

    def to_a2a_skills(
        self,
        reputation: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate the ``skills`` section for the A2A agent card.

        Only tools with ``show_on_agent_card=True`` are included — this is a
        public discoverability surface, not the full tool inventory (see
        ``GET /agent/tools`` and ``to_mcp_server_card_tools`` for that).
        """
        skills: list[dict[str, Any]] = []
        for tool in self.list_latest(include_deprecated=True):
            if not tool.show_on_agent_card:
                continue
            skill: dict[str, Any] = {
                "id": tool.name,
                "name": tool.name,
                "description": tool.description,
                "tags": tool.tags,
                "version": tool.version,
            }
            if tool.examples:
                skill["examples"] = list(tool.examples)
            if tool.use_when:
                skill["use_when"] = tool.use_when
            if tool.limitations:
                skill["limitations"] = tool.limitations
            if tool.alternatives:
                skill["alternatives"] = list(tool.alternatives)
            if tool.deprecated:
                skill["deprecated"] = True
                if tool.superseded_by:
                    skill["superseded_by"] = tool.superseded_by
            metrics = (reputation or {}).get(f"platform/{tool.name}")
            if _has_reputation_signal(metrics):
                skill["reputation"] = dict(metrics)
            skills.append(skill)
        return skills

    def to_a2a_tool_list(
        self,
        reputation: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate a detailed ``tools`` section with JSON Schema for the A2A card.

        Only tools with ``show_on_agent_card=True`` are included (see
        ``to_a2a_skills`` for rationale).
        """
        tools: list[dict[str, Any]] = []
        for tool in self.list_latest(include_deprecated=True):
            if not tool.show_on_agent_card:
                continue
            entry: dict[str, Any] = {
                "name": tool.name,
                "version": tool.version,
                "description": tool.description,
                "tags": tool.tags,
                "input_schema": tool.input_schema.model_json_schema(),
            }
            if tool.use_when:
                entry["use_when"] = tool.use_when
            if tool.limitations:
                entry["limitations"] = tool.limitations
            if tool.alternatives:
                entry["alternatives"] = list(tool.alternatives)
            if tool.output_schema is not None:
                if isinstance(tool.output_schema, dict):
                    entry["output_schema"] = tool.output_schema
                else:
                    entry["output_schema"] = tool.output_schema.model_json_schema()
            if tool.deprecated:
                entry["deprecated"] = True
            metrics = (reputation or {}).get(f"platform/{tool.name}")
            if _has_reputation_signal(metrics):
                entry["reputation"] = dict(metrics)
            tools.append(entry)
        return tools

    # ── Export: MCP ───────────────────────────────────────────────────────────

    def to_mcp_server_card_tools(
        self,
        reputation: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate the tools array for the static .well-known/mcp/server-card.json."""
        tools: list[dict[str, Any]] = []
        for tool in self.list_latest():
            title = tool.name.replace("_", " ").title()
            entry: dict[str, Any] = {
                "name": tool.name,
                "title": title,
                "description": tool.description,
                "inputSchema": tool.input_schema.model_json_schema(),
                "annotations": tool.annotations or {"readOnlyHint": True},
            }
            if tool.use_when:
                entry["use_when"] = tool.use_when
            if tool.limitations:
                entry["limitations"] = tool.limitations
            if tool.alternatives:
                entry["alternatives"] = list(tool.alternatives)
            if tool.output_schema is not None:
                if isinstance(tool.output_schema, dict):
                    entry["outputSchema"] = tool.output_schema
                else:
                    entry["outputSchema"] = tool.output_schema.model_json_schema()
            metrics = (reputation or {}).get(f"platform/{tool.name}")
            if _has_reputation_signal(metrics):
                entry["reputation"] = dict(metrics)
            tools.append(entry)
        return tools

    def to_mcp_tool_defs(
        self,
        reputation: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return metadata dicts suitable for dynamic MCP tool registration."""
        defs: list[dict[str, Any]] = []
        for tool in self.list_latest():
            raw_output_schema = None
            output_model = None
            if tool.output_schema is not None:
                if isinstance(tool.output_schema, dict):
                    raw_output_schema = tool.output_schema
                else:
                    raw_output_schema = tool.output_schema.model_json_schema()
                    output_model = tool.output_schema

            if _mcp_safe_output_schema(raw_output_schema) is None:
                output_model = None

            metrics = (reputation or {}).get(f"platform/{tool.name}")
            description = format_mcp_quality_description(tool.description, metrics)

            # Agent guidance stays in the description for LLM-facing clients.
            if tool.use_when:
                description = f"{description}\n\nUse when: {tool.use_when}"
            if tool.limitations:
                description = f"{description}\n\nLimitations: {tool.limitations}"
            if tool.alternatives:
                description = f"{description}\n\nAlternatives: {', '.join(tool.alternatives)}"

            defs.append(
                {
                    "name": tool.name,
                    "title": tool.name.replace("_", " ").title(),
                    "description": description,
                    "input_schema": tool.input_schema,
                    "output_schema": _mcp_safe_output_schema(raw_output_schema),
                    "output_model": output_model,
                    "annotations": tool.annotations or {"readOnlyHint": True},
                    "meta": build_reputation_meta(metrics),
                    "implementation": tool.implementation,
                }
            )
        return defs

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _get_latest(
        bucket: dict[str, ToolDefinition],
        *,
        include_deprecated: bool = False,
    ) -> ToolDefinition | None:
        """Return the highest-semver non-deprecated tool in a name bucket."""
        candidates = [t for t in bucket.values() if include_deprecated or not t.deprecated]
        if not candidates:
            return None
        candidates.sort(key=lambda t: t.parsed_version, reverse=True)
        return candidates[0]
