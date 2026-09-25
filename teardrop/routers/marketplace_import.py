# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace MCP import routes: preview and publish remote MCP tools as marketplace org tools.

Extracted verbatim from ``teardrop.routers.marketplace`` with no logic changes.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing import get_current_pricing
from marketplace import get_author_config
from marketplace.models import MarketplaceCategory
from mcp_client import discover_mcp_tools, get_org_mcp_server
from org_tools import create_org_tool, list_org_tools, validate_safe_schema_subset
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth, require_org_machine
from teardrop.rate_limit import _enforce_rate_limit
from tools import registry
from tools.schema import normalize_to_safe_schema_subset

router = APIRouter()

_IMPORT_TOOL_NAME_PATTERN = re.compile(r"[^a-z0-9]+")


class MarketplaceImportPreviewRequest(BaseModel):
    server_id: str = Field(..., min_length=1, max_length=128)
    tool_names: list[str] | None = None


class MarketplaceImportPublishToolRequest(BaseModel):
    remote_tool_name: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(..., min_length=1, max_length=500)
    input_schema: dict | None = Field(default=None, description="Normalized Draft 7 schema for tool inputs")
    output_schema: dict | None = Field(default=None, description="Confirmed Draft 7 schema for tool outputs")
    marketplace_description: str | None = Field(default=None, max_length=1000)
    category: MarketplaceCategory = ""
    base_price_usdc: int = Field(default=0, ge=0, le=100_000_000)


class MarketplaceImportPublishRequest(BaseModel):
    server_id: str = Field(..., min_length=1, max_length=128)
    tools: list[MarketplaceImportPublishToolRequest] = Field(..., min_length=1, max_length=50)


class ImportPreviewSchemaStatus(BaseModel):
    input: str = Field(..., description="'supported', 'partial', or 'synthesized'.")
    output: str = Field(..., description="'supported', 'partial', or 'synthesized'.")


class ImportPreviewDroppedFeatures(BaseModel):
    input: list[str] = Field(default_factory=list, description="Input schema features dropped during normalization.")
    output: list[str] = Field(default_factory=list, description="Output schema features dropped during normalization.")


class MarketplaceImportPreviewTool(BaseModel):
    remote_tool_name: str = Field(..., description="Tool name as reported by the remote MCP server.")
    proposed_name: str = Field(..., description="Teardrop-compatible name proposed for publishing.")
    description: str
    marketplace_description: str
    input_schema: dict[str, Any] = Field(..., description="Normalized Draft 7 input schema.")
    output_schema: dict[str, Any] = Field(..., description="Normalized (or synthesized) Draft 7 output schema.")
    schema_status: ImportPreviewSchemaStatus
    dropped_schema_features: ImportPreviewDroppedFeatures
    name_adjusted: bool = Field(..., description="True if the proposed name differs from the remote tool name.")
    name_collision_resolved: bool = Field(..., description="True if the name was renamed to resolve a collision.")
    quota_exceeded: bool = Field(..., description="True if importing this tool would exceed the org tool quota.")
    publishable: bool = Field(..., description="Shortcut for 'not quota_exceeded'.")
    suggested_base_price_usdc: int = Field(..., description="Recommended marketplace price in atomic USDC.")
    category: str = Field(default="", description="Suggested marketplace category; blank during preview.")
    warnings: list[str] = Field(default_factory=list, description="Human-readable notes about schema/name adjustments.")


class MarketplaceImportPreviewError(BaseModel):
    remote_tool_name: str
    status_code: int
    error: str


class MarketplaceImportPreviewResponse(BaseModel):
    server_id: str
    slots_remaining: int = Field(..., description="Org tool slots remaining before hitting the quota.")
    can_publish: bool = Field(..., description="False if the caller/org cannot currently publish (see blockers).")
    blockers: list[str] = Field(default_factory=list, description="Reasons publishing is blocked, e.g. 'requires_org_machine'.")
    tools: list[MarketplaceImportPreviewTool]
    errors: list[MarketplaceImportPreviewError]


class MarketplaceImportPublishedTool(BaseModel):
    id: str
    name: str
    org_id: str
    publish_as_mcp: bool
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    base_price_usdc: int


class MarketplaceImportPublishCreatedItem(BaseModel):
    remote_tool_name: str
    tool: MarketplaceImportPublishedTool


class MarketplaceImportPublishError(BaseModel):
    remote_tool_name: str
    name: str
    status_code: int
    error: str


class MarketplaceImportPublishResponse(BaseModel):
    server_id: str
    created: list[MarketplaceImportPublishCreatedItem]
    errors: list[MarketplaceImportPublishError]


def _sanitize_import_tool_name(value: str) -> str:
    candidate = _IMPORT_TOOL_NAME_PATTERN.sub("_", value.strip().lower()).strip("_")
    candidate = re.sub(r"_+", "_", candidate)
    if not candidate:
        candidate = "tool"
    if not candidate[0].isalpha():
        candidate = f"tool_{candidate}"
    candidate = candidate[:64].rstrip("_")
    return candidate or "tool"


def _propose_import_tool_name(remote_name: str, reserved_names: set[str]) -> tuple[str, bool, bool]:
    base_name = _sanitize_import_tool_name(remote_name)
    candidate = base_name
    name_adjusted = candidate != remote_name
    collision_resolved = False
    suffix = 2

    while candidate in reserved_names or registry.get(candidate) is not None:
        collision_resolved = True
        name_adjusted = True
        suffix_text = f"_{suffix}"
        candidate = f"{base_name[: max(1, 64 - len(suffix_text))]}{suffix_text}"
        suffix += 1

    reserved_names.add(candidate)
    return candidate, name_adjusted, collision_resolved


def _synthesized_output_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "description": (
            description.strip() or "Synthesized output schema for imported MCP tool. Review and refine before publishing."
        ),
    }


def _validate_import_schema(schema: dict[str, Any], field_name: str) -> None:
    from jsonschema import Draft7Validator, SchemaError

    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"Invalid {field_name}: {exc.message}")

    subset_errors = validate_safe_schema_subset(schema)
    if subset_errors:
        raise ValueError(f"Unsupported {field_name} features: {'; '.join(subset_errors[:5])}")


def _classify_import_publish_error(message: str) -> int:
    lowered = message.lower()
    if "settlement wallet" in lowered or "already exists" in lowered or "built-in tool" in lowered:
        return status.HTTP_409_CONFLICT
    if "limit reached" in lowered or "invalid" in lowered or "unsupported" in lowered or "required" in lowered:
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _schema_status(dropped: list[str], *, synthesized: bool = False) -> str:
    if synthesized:
        return "synthesized"
    if dropped:
        return "normalized"
    return "unchanged"


def _normalized_import_schemas(
    discovered_tool: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str], bool]:
    description = str(discovered_tool.get("description") or "")
    normalized_input_schema, input_dropped = normalize_to_safe_schema_subset(discovered_tool.get("input_schema") or {})

    raw_output_schema = discovered_tool.get("output_schema")
    if isinstance(raw_output_schema, dict) and raw_output_schema:
        normalized_output_schema, output_dropped = normalize_to_safe_schema_subset(raw_output_schema)
        output_synthesized = False
    else:
        normalized_output_schema = _synthesized_output_schema(description)
        output_dropped = []
        output_synthesized = True

    return normalized_input_schema, normalized_output_schema, input_dropped, output_dropped, output_synthesized


def _preview_import_tool(
    discovered_tool: dict[str, Any],
    reserved_names: set[str],
    *,
    suggested_base_price_usdc: int,
    quota_exceeded: bool,
) -> dict[str, Any]:
    remote_tool_name = str(discovered_tool.get("name") or "tool")
    description = str(discovered_tool.get("description") or "")
    proposed_name, name_adjusted, collision_resolved = _propose_import_tool_name(remote_tool_name, reserved_names)
    normalized_input_schema, normalized_output_schema, input_dropped, output_dropped, output_synthesized = (
        _normalized_import_schemas(discovered_tool)
    )

    warnings: list[str] = []
    if input_dropped:
        warnings.append("input_schema was normalized to Teardrop's safe subset")
    if output_synthesized:
        warnings.append("output_schema was synthesized because the MCP server did not expose one")
    elif output_dropped:
        warnings.append("output_schema was normalized to Teardrop's safe subset")
    if name_adjusted:
        warnings.append("proposed name was adjusted to satisfy Teardrop naming or collision rules")
    if quota_exceeded:
        warnings.append("publishing this tool would exceed the organisation tool quota")

    return {
        "remote_tool_name": remote_tool_name,
        "proposed_name": proposed_name,
        "description": description,
        "marketplace_description": description,
        "input_schema": normalized_input_schema,
        "output_schema": normalized_output_schema,
        "schema_status": {
            "input": _schema_status(input_dropped),
            "output": _schema_status(output_dropped, synthesized=output_synthesized),
        },
        "dropped_schema_features": {
            "input": input_dropped,
            "output": output_dropped,
        },
        "name_adjusted": name_adjusted,
        "name_collision_resolved": collision_resolved,
        "quota_exceeded": quota_exceeded,
        "publishable": not quota_exceeded,
        "suggested_base_price_usdc": suggested_base_price_usdc,
        "category": "",
        "warnings": warnings,
    }


@router.post("/marketplace/import/preview", tags=["Marketplace"], response_model=MarketplaceImportPreviewResponse)
async def preview_marketplace_import(
    body: MarketplaceImportPreviewRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Preview MCP tools importable from one of the org's registered MCP servers."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    await _enforce_rate_limit(
        f"marketplace:import:preview:{org_id}",
        s.rate_limit_mcp_discover_rpm,
        detail="Rate limit exceeded for marketplace import preview.",
    )

    srv = await get_org_mcp_server(body.server_id, org_id)
    if srv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")

    try:
        discovered_tools = await discover_mcp_tools(srv)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to MCP server: {type(exc).__name__}",
        )

    discovered_by_name = {str(tool.get("name")): tool for tool in discovered_tools}
    requested_names = body.tool_names or list(discovered_by_name.keys())
    existing_tools = await list_org_tools(org_id)
    reserved_names = {tool.name for tool in existing_tools}
    slots_remaining = max(0, s.max_org_tools - len(existing_tools))
    pricing = await get_current_pricing()
    suggested_base_price_usdc = pricing.tool_call_cost if pricing is not None else 0

    author_config = await get_author_config(org_id)
    blockers: list[str] = []
    try:
        await require_org_machine(payload)
    except HTTPException:
        blockers.append("requires_org_machine")
    if author_config is None:
        blockers.append("settlement_wallet_missing")
    can_publish = not blockers

    preview_tools: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, remote_tool_name in enumerate(requested_names):
        discovered_tool = discovered_by_name.get(remote_tool_name)
        if discovered_tool is None:
            errors.append(
                {
                    "remote_tool_name": remote_tool_name,
                    "status_code": status.HTTP_404_NOT_FOUND,
                    "error": "Tool not found on MCP server.",
                }
            )
            continue
        preview_tools.append(
            _preview_import_tool(
                discovered_tool,
                reserved_names,
                suggested_base_price_usdc=suggested_base_price_usdc,
                quota_exceeded=index >= slots_remaining,
            )
        )

    return JSONResponse(
        content={
            "server_id": body.server_id,
            "slots_remaining": slots_remaining,
            "can_publish": can_publish,
            "blockers": blockers,
            "tools": preview_tools,
            "errors": errors,
        }
    )


@router.post("/marketplace/import/publish", tags=["Marketplace"], response_model=MarketplaceImportPublishResponse)
async def publish_marketplace_import(
    body: MarketplaceImportPublishRequest,
    payload: dict = Depends(require_org_machine),
) -> JSONResponse:
    """Publish selected MCP tools as marketplace-visible MCP-backed org tools."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    user_id: str = payload.get("sub", "")

    await _enforce_rate_limit(
        f"marketplace:import:publish:{org_id}",
        s.rate_limit_org_mcp_rpm,
        detail="Rate limit exceeded for marketplace import publish.",
    )

    srv = await get_org_mcp_server(body.server_id, org_id)
    if srv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")

    try:
        discovered_tools = await discover_mcp_tools(srv)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to MCP server: {type(exc).__name__}",
        )

    discovered_by_name = {str(tool.get("name")): tool for tool in discovered_tools}
    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for item in body.tools:
        remote_tool_name = item.remote_tool_name
        discovered_tool = discovered_by_name.get(remote_tool_name)
        if discovered_tool is None:
            errors.append(
                {
                    "remote_tool_name": remote_tool_name,
                    "name": item.name,
                    "status_code": status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "error": "Tool not found on MCP server.",
                }
            )
            continue

        if registry.get(item.name) is not None:
            errors.append(
                {
                    "remote_tool_name": remote_tool_name,
                    "name": item.name,
                    "status_code": status.HTTP_409_CONFLICT,
                    "error": f"Tool name '{item.name}' conflicts with a built-in tool.",
                }
            )
            continue

        try:
            normalized_input_schema, normalized_output_schema, _, _, _ = _normalized_import_schemas(discovered_tool)
            input_schema = item.input_schema if item.input_schema is not None else normalized_input_schema
            output_schema = item.output_schema if item.output_schema is not None else normalized_output_schema
            _validate_import_schema(input_schema, "input_schema")
            _validate_import_schema(output_schema, "output_schema")
            created_tool = await create_org_tool(
                org_id=org_id,
                name=item.name,
                description=item.description,
                input_schema=input_schema,
                output_schema=output_schema,
                webhook_url=None,
                auth_header_name=None,
                auth_header_value=None,
                timeout_seconds=srv.timeout_seconds,
                actor_id=user_id,
                publish_as_mcp=True,
                marketplace_description=item.marketplace_description or item.description,
                category=item.category,
                base_price_usdc=item.base_price_usdc,
                mcp_server_id=body.server_id,
                mcp_tool_name=remote_tool_name,
            )
        except Exception as exc:
            status_code = (
                _classify_import_publish_error(str(exc)) if isinstance(exc, ValueError) else status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            message = {
                status.HTTP_409_CONFLICT: "Tool already exists or settlement wallet is not configured.",
                status.HTTP_422_UNPROCESSABLE_ENTITY: "Invalid tool definition or organization tool limit reached.",
            }.get(status_code, "Tool publishing failed.")
            errors.append(
                {
                    "remote_tool_name": remote_tool_name,
                    "name": item.name,
                    "status_code": status_code,
                    "error": message,
                }
            )
            continue

        created.append(
            {
                "remote_tool_name": remote_tool_name,
                "tool": {
                    "id": created_tool.id,
                    "name": created_tool.name,
                    "org_id": created_tool.org_id,
                    "publish_as_mcp": created_tool.publish_as_mcp,
                    "mcp_server_id": created_tool.mcp_server_id,
                    "mcp_tool_name": created_tool.mcp_tool_name,
                    "base_price_usdc": created_tool.base_price_usdc,
                },
            }
        )

    if created:
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"server_id": body.server_id, "created": created, "errors": errors},
        )

    response_status = status.HTTP_400_BAD_REQUEST
    if errors:
        response_status = max((error["status_code"] for error in errors), default=response_status)

    return JSONResponse(
        status_code=response_status,
        content={"server_id": body.server_id, "created": created, "errors": errors},
    )
