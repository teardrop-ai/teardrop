# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared capability manifest behind Teardrop's public discovery surfaces.

One descriptor per callable capability: active registry (platform) tools plus
published community marketplace tools. ``tools.mcp_server.refresh_mcp_tools``
rebuilds the snapshot from the same reputation, price, and catalog inputs it
registers live MCP tools with; surfaces project it into their own wire format.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from tools import registry
from tools.registry import _has_reputation_signal, build_mcp_tool_meta, build_reputation_meta


class Capability(BaseModel):
    """Protocol-neutral description of one callable capability."""

    model_config = ConfigDict(frozen=True)

    name: str  # tools/call name: bare for platform tools, ``org_slug/tool`` for community tools
    qualified_name: str
    kind: Literal["platform", "community"]
    version: str = ""
    title: str | None = None
    description: str
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    use_when: str = ""
    limitations: str = ""
    alternatives: tuple[str, ...] = ()
    cost_usdc: int | None = None  # atomic USDC per call; None when pricing is unavailable
    x402_payable: bool
    reputation: dict[str, Any] | None = None  # None until the tool has observed calls
    show_on_agent_card: bool = True
    deprecated: bool = False  # listed for lifecycle signalling only; not callable
    superseded_by: str | None = None


def _observed(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    return dict(metrics) if metrics and _has_reputation_signal(metrics) else None


def build_capability_manifest(
    reputation: dict[str, dict[str, Any]] | None = None,
    prices: dict[str, int] | None = None,
    community: Iterable[Any] = (),
) -> tuple[Capability, ...]:
    """Build descriptors from the registry plus marketplace catalog entries.

    ``community`` takes ``MarketplaceTool`` rows; platform rows are skipped because
    the registry is authoritative for first-party tools.
    """
    reputation = reputation or {}
    capabilities: list[Capability] = []
    active = {tool.name: tool for tool in registry.list_latest()}
    # Active latest per name; a fully deprecated name keeps its newest version, flagged.
    for latest in registry.list_latest(include_deprecated=True):
        tool = active.get(latest.name, latest)
        output_schema = tool.output_schema
        if output_schema is not None and not isinstance(output_schema, dict):
            output_schema = output_schema.model_json_schema()
        capabilities.append(
            Capability(
                name=tool.name,
                qualified_name=f"platform/{tool.name}",
                kind="platform",
                version=tool.version,
                title=tool.name.replace("_", " ").title(),
                description=tool.description,
                tags=tuple(tool.tags),
                examples=tuple(tool.examples),
                input_schema=tool.input_schema.model_json_schema(),
                output_schema=output_schema,
                annotations=tool.annotations or {"readOnlyHint": True},
                use_when=tool.use_when,
                limitations=tool.limitations,
                alternatives=tuple(tool.alternatives),
                cost_usdc=prices.get(tool.name) if prices is not None else None,
                x402_payable=True,
                reputation=_observed(reputation.get(f"platform/{tool.name}")),
                show_on_agent_card=tool.show_on_agent_card,
                deprecated=tool.deprecated,
                superseded_by=tool.superseded_by,
            )
        )
    for tool in community:
        if tool.tool_type != "community":
            continue
        capabilities.append(
            Capability(
                name=tool.qualified_name,
                qualified_name=tool.qualified_name,
                kind="community",
                description=tool.marketplace_description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                cost_usdc=tool.cost_usdc,
                # Credit-only until anonymous payers get earnings attribution; mirrors mcp_gateway._x402_payable.
                x402_payable=False,
                reputation=_observed(reputation.get(tool.qualified_name)),
            )
        )
    return tuple(capabilities)


_snapshot: tuple[Capability, ...] | None = None


def set_capability_manifest(capabilities: tuple[Capability, ...] | None) -> None:
    global _snapshot
    _snapshot = capabilities


def get_capability_manifest() -> tuple[Capability, ...]:
    """Return the last refreshed manifest, or a registry-only one before the first refresh."""
    return _snapshot if _snapshot is not None else build_capability_manifest()


def capability_meta(capability: Capability) -> dict[str, Any] | None:
    """MCP ``_meta`` block (reputation plus price when known) shared by tools/list and the server card."""
    if capability.cost_usdc is None:
        return build_reputation_meta(capability.reputation)
    return build_mcp_tool_meta(capability.reputation, capability.cost_usdc)


def to_mcp_server_card_tool(capability: Capability) -> dict[str, Any]:
    """Project a capability into a ``/.well-known/mcp/server-card.json`` tool entry."""
    entry: dict[str, Any] = {
        "name": capability.name,
        "description": capability.description,
        "inputSchema": capability.input_schema,
    }
    if capability.title:
        entry["title"] = capability.title
    if capability.annotations:
        entry["annotations"] = capability.annotations
    entry.update(_guidance(capability))
    if capability.output_schema is not None:
        entry["outputSchema"] = capability.output_schema
    if capability.reputation is not None:
        entry["reputation"] = dict(capability.reputation)
    meta = capability_meta(capability)
    if meta:
        entry["_meta"] = meta
    return entry


def _guidance(capability: Capability) -> dict[str, Any]:
    guidance: dict[str, Any] = {}
    if capability.use_when:
        guidance["use_when"] = capability.use_when
    if capability.limitations:
        guidance["limitations"] = capability.limitations
    if capability.alternatives:
        guidance["alternatives"] = list(capability.alternatives)
    return guidance


def to_a2a_skill(capability: Capability) -> dict[str, Any]:
    """Project a capability into an A2A agent-card ``skills`` entry."""
    skill: dict[str, Any] = {
        "id": capability.name,
        "name": capability.name,
        "description": capability.description,
        "tags": list(capability.tags),
        "version": capability.version,
    }
    if capability.examples:
        skill["examples"] = list(capability.examples)
    skill.update(_guidance(capability))
    if capability.deprecated:
        skill["deprecated"] = True
        if capability.superseded_by:
            skill["superseded_by"] = capability.superseded_by
    if capability.reputation is not None:
        skill["reputation"] = dict(capability.reputation)
    return skill


def format_atomic_usdc(amount_usdc: int) -> str:
    whole, fractional = divmod(max(0, int(amount_usdc)), 1_000_000)
    return f"${whole}.{fractional:06d}"


def escape_llms_text(value: Any) -> str:
    """Flatten text to one markdown-safe line for llms.txt surfaces."""
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("`", "'")
        .replace("[", "(")
        .replace("]", ")")
        .replace("<", "(")
        .replace(">", ")")
        .replace("#", "")
        .replace("|", "-")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def to_llms_txt_line(capability: Capability) -> str:
    """Project a capability into one ``llms.txt`` list item; price shown only when a positive per-call charge is known."""
    price = f" ({format_atomic_usdc(capability.cost_usdc)} per call)" if capability.cost_usdc else ""
    return f"- {escape_llms_text(capability.name)}{price}: {escape_llms_text(capability.description)}"


def to_x402_mcp_listing(capability: Capability) -> dict[str, Any]:
    """Project a priced capability into a ``/.well-known/x402`` MCP tool listing (exact scheme only)."""
    return {
        "name": capability.name,
        "description": capability.description,
        "scheme": "exact",
        "amount_usdc": capability.cost_usdc,
    }


def to_a2a_tool(capability: Capability) -> dict[str, Any]:
    """Project a capability into an A2A agent-card ``tools`` entry with JSON Schema."""
    entry: dict[str, Any] = {
        "name": capability.name,
        "version": capability.version,
        "description": capability.description,
        "tags": list(capability.tags),
        "input_schema": capability.input_schema,
    }
    entry.update(_guidance(capability))
    if capability.output_schema is not None:
        entry["output_schema"] = capability.output_schema
    if capability.deprecated:
        entry["deprecated"] = True
    if capability.reputation is not None:
        entry["reputation"] = dict(capability.reputation)
    return entry
