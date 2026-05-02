# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Versioned tool registry for Teardrop.

Provides ToolDefinition (the canonical way to declare a tool) and
ToolRegistry (the singleton that holds all registered tools and
exports them as LangChain tools, A2A skills, and MCP definitions).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from langchain_core.tools import StructuredTool
from packaging.version import Version
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ToolDefinition(BaseModel):
    """Canonical description of a single versioned tool."""

    name: str = Field(..., description="Unique tool identifier, e.g. 'web_search'")
    version: str = Field(..., description="Semver string, e.g. '1.0.0'")
    description: str = Field(..., description="Human/agent-readable description")
    tags: list[str] = Field(default_factory=list, description="Categorisation tags")
    input_schema: Any = Field(..., description="Pydantic BaseModel class for input validation")
    output_schema: Any = Field(default=None, description="Optional Pydantic BaseModel class for output validation")
    timeout_seconds: float | None = Field(default=None, description="Optional per-call timeout override")
    max_calls_per_run: int | None = Field(default=None, description="Optional per-run call cap")
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

    def to_a2a_skills(self) -> list[dict[str, Any]]:
        """Generate the ``skills`` section for the A2A agent card."""
        skills: list[dict[str, Any]] = []
        for tool in self.list_latest(include_deprecated=True):
            skill: dict[str, Any] = {
                "name": tool.name,
                "description": tool.description,
                "tags": tool.tags,
                "version": tool.version,
            }
            if tool.deprecated:
                skill["deprecated"] = True
                if tool.superseded_by:
                    skill["superseded_by"] = tool.superseded_by
            skills.append(skill)
        return skills

    def to_a2a_tool_list(self) -> list[dict[str, Any]]:
        """Generate a detailed ``tools`` section with JSON Schema for the A2A card."""
        tools: list[dict[str, Any]] = []
        for tool in self.list_latest(include_deprecated=True):
            entry: dict[str, Any] = {
                "name": tool.name,
                "version": tool.version,
                "description": tool.description,
                "tags": tool.tags,
                "input_schema": tool.input_schema.model_json_schema(),
            }
            if tool.output_schema is not None:
                entry["output_schema"] = tool.output_schema.model_json_schema()
            if tool.deprecated:
                entry["deprecated"] = True
            tools.append(entry)
        return tools

    # ── Export: MCP ───────────────────────────────────────────────────────────

    def to_mcp_tool_defs(self) -> list[dict[str, Any]]:
        """Return metadata dicts suitable for dynamic MCP tool registration."""
        defs: list[dict[str, Any]] = []
        for tool in self.list_latest():
            defs.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
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
