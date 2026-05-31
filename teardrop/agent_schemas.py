# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Request models and tool-exclusion helpers for the agent run endpoint.

These were extracted verbatim from ``teardrop.routers.agent`` so the run
request schema (``AgentRunRequest``), the per-run tool exclusion policy
(``ToolPolicy``), and the qualified-name normaliser are independently
importable and testable. ``teardrop.routers.agent`` re-imports them, so the
public import surface is unchanged.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

# API-facing qualified tool names carry a namespace prefix (``platform/`` for
# built-in tools, ``org/`` for an org's own webhook tools). The internal
# executor/binder keys are unprefixed, so exclusions must be normalised before
# matching. Third-party marketplace tools keep their fully-qualified
# ``{org_slug}/{tool_name}`` form and are intentionally left untouched.
_TOOL_NAMESPACE_PREFIXES = ("platform/", "org/")


def _normalize_exclusion_name(name: str) -> str:
    """Map API-facing qualified names to internal executor/binder tool keys."""
    for prefix in _TOOL_NAMESPACE_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


class ToolPolicy(BaseModel):
    exclude_names: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Qualified tool names to exclude for this run.",
    )

    @field_validator("exclude_names")
    @classmethod
    def _validate_exclude_names(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            stripped = value.strip()
            if not stripped:
                raise ValueError("exclude_names entries must be non-empty strings")
            if len(stripped) > 200:
                raise ValueError("exclude_names entries must be 200 characters or fewer")
            normalized.append(stripped)
        return normalized


class AgentRunRequest(BaseModel):
    message: str = Field(..., description="User message to send to the agent", max_length=4096)
    thread_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Conversation thread ID for multi-turn sessions",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional extra context passed to the agent state metadata",
    )
    emit_ui: bool = Field(
        default=True,
        description="Whether to generate structured UI components in the final output.",
    )
    tool_policy: ToolPolicy | None = Field(
        default=None,
        description="Optional per-run tool exclusion policy.",
    )
