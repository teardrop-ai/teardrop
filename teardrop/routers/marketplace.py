# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace REST routes: author config/earnings/withdrawals, public catalog
browsing, and subscriptions.

Sub-domains (each with its own section below):
  1. Author Config          — set/get settlement wallet for payouts
  2. MCP Import & Publish   — preview and publish external MCP tools to catalog
  3. Author Earnings        — balance, history, per-tool aggregates (atomic USDC)
  4. Author Withdrawals     — request payout to settlement wallet
  5. Public Catalog         — browse/search marketplace, author discovery
  6. Subscriptions          — subscribe/unsubscribe to published tools

Extracted verbatim from ``teardrop.app`` with no logic changes. Billing, x402,
SSRF, circuit-breaker, and subscription-gate semantics are preserved exactly.
The MCP JSON-RPC gateway (POST /mcp/v1) lives in ``teardrop.routers.marketplace_mcp``.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing import (
    apply_platform_fee,
    get_current_pricing,
    get_invoice_by_run,
    get_live_pricing,
    get_tool_pricing_overrides,
    resolve_tool_cost,
)
from marketplace import (
    _build_agent_cursor,
    _decode_agent_cursor,
    delete_agent_registration,
    get_agent_directory,
    get_agent_registration,
    get_author_balance,
    get_author_config,
    get_author_earnings_by_tool,
    get_author_earnings_history,
    get_marketplace_author_summary,
    get_marketplace_catalog,
    get_marketplace_catalog_tool,
    record_run_feedback,
    request_withdrawal,
    set_agent_registration,
    set_author_config,
)
from marketplace import list_marketplace_authors as list_marketplace_authors_data
from marketplace.models import MarketplaceCategory
from mcp_client import discover_mcp_tools, get_org_mcp_server
from org_tools import create_org_tool, list_org_tools, validate_safe_schema_subset
from teardrop.config import get_settings
from teardrop.dependencies import (
    _require_org_id,
    require_auth,
    require_org_admin,
    require_org_machine,
    require_settlement_wallet_auth,
)
from teardrop.rate_limit import _enforce_rate_limit
from tools import registry
from tools.shared import normalize_to_safe_schema_subset

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

_MARKETPLACE_VALID_CATEGORIES = {"", "defi", "search", "data", "communication", "utility"}
_IMPORT_TOOL_NAME_PATTERN = re.compile(r"[^a-z0-9]+")


# ─── MCP Marketplace – REST API ──────────────────────────────────────────────


class SetAuthorConfigRequest(BaseModel):
    settlement_wallet: str = Field(..., min_length=42, max_length=42)


class MarketplaceAuthorConfigResponse(BaseModel):
    org_id: str
    settlement_wallet: str | None = None
    created_at: str | None = Field(default=None, description="ISO 8601 timestamp; null if unconfigured.")
    updated_at: str | None = Field(default=None, description="ISO 8601 timestamp; null if unconfigured.")


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
    blockers: list[str] = Field(default_factory=list, description="Reasons publishing is blocked, e.g. 'requires_org_admin'.")
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
    return status.HTTP_400_BAD_REQUEST


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


# ─── Author Config (settlement wallet payout destination) ─────────────────


@router.post("/marketplace/author-config", tags=["Marketplace"], response_model=MarketplaceAuthorConfigResponse)
async def set_marketplace_author_config(
    body: SetAuthorConfigRequest,
    payload: dict = Depends(require_settlement_wallet_auth),
) -> JSONResponse:
    """Configure or update the marketplace author settings for the org.

    Admins or the owning SIWE wallet may configure the payout destination.
    """
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)

    if payload.get("role") != "admin":
        from marketplace.models import normalize_eip55_address

        requested_wallet, wallet_error = normalize_eip55_address(body.settlement_wallet)
        authenticated_address = payload.get("address")
        if isinstance(authenticated_address, str):
            authenticated_wallet, authenticated_error = normalize_eip55_address(authenticated_address)
        else:
            authenticated_wallet, authenticated_error = None, "Missing authenticated wallet"
        if wallet_error is not None or authenticated_error is not None or requested_wallet != authenticated_wallet:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Settlement wallet must match the authenticated wallet.",
            )

    try:
        config = await set_author_config(
            org_id=org_id,
            settlement_wallet=body.settlement_wallet,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    logger.info(
        "marketplace_settlement_wallet_set org=%s by=%s wallet=%s",
        org_id,
        payload["sub"],
        f"{config.settlement_wallet[:6]}...{config.settlement_wallet[-4:]}",
    )

    return JSONResponse(
        content={
            "org_id": config.org_id,
            "settlement_wallet": config.settlement_wallet,
            "created_at": config.created_at.isoformat(),
            "updated_at": config.updated_at.isoformat(),
        }
    )


@router.get("/marketplace/author-config", tags=["Marketplace"], response_model=MarketplaceAuthorConfigResponse)
async def get_marketplace_author_config_endpoint(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get the marketplace author configuration for the authenticated org."""
    org_id = _require_org_id(payload)

    config = await get_author_config(org_id)
    if config is None:
        return JSONResponse(
            content={
                "org_id": org_id,
                "settlement_wallet": None,
                "created_at": None,
                "updated_at": None,
            }
        )

    return JSONResponse(
        content={
            "org_id": config.org_id,
            "settlement_wallet": config.settlement_wallet,
            "created_at": config.created_at.isoformat(),
            "updated_at": config.updated_at.isoformat(),
        }
        # ─── MCP Import & Publish (preview + publish remote MCP tools) ────────────
    )


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

    # Surface publish blockers up front so non-admin or unconfigured authors
    # learn why /marketplace/import/publish would reject them, instead of
    # discovering it only after preparing a publish payload. Additive fields.
    author_config = await get_author_config(org_id)
    is_org_admin = payload.get("role") == "admin"
    blockers: list[str] = []
    if not is_org_admin:
        blockers.append("requires_org_admin")
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
    payload: dict = Depends(require_org_admin),
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
            message = str(exc)
            status_code = _classify_import_publish_error(message)
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
        # ─── Author Earnings & Balance (atomic USDC ledger) ──────────────────────
    )


class MarketplaceBalanceResponse(BaseModel):
    org_id: str
    balance_usdc: int


@router.get("/marketplace/balance", tags=["Marketplace"], response_model=MarketplaceBalanceResponse)
async def get_marketplace_balance(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get the pending (unwithdrawn) earnings balance for the authenticated org."""
    org_id = _require_org_id(payload)

    balance = await get_author_balance(org_id)
    return JSONResponse(content={"org_id": org_id, "balance_usdc": balance})


class MarketplaceEarningEntry(BaseModel):
    id: str
    tool_name: str
    caller_org_id: str
    total_cost_usdc: int
    author_share_usdc: int
    platform_share_usdc: int
    status: str
    created_at: str = Field(..., description="ISO 8601 timestamp.")


class MarketplaceEarningsResponse(BaseModel):
    earnings: list[MarketplaceEarningEntry]
    next_cursor: str | None = None


@router.get("/marketplace/earnings", tags=["Marketplace"], response_model=MarketplaceEarningsResponse)
async def get_marketplace_earnings(
    payload: dict = Depends(require_auth),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    tool_name: str | None = Query(default=None, max_length=64),
) -> JSONResponse:
    """Get paginated earnings history for the authenticated org.

    Optionally filter by ``tool_name`` to see earnings for a specific tool.
    """
    from shared.pagination import parse_cursor

    org_id = _require_org_id(payload)
    cursor_dt = parse_cursor(cursor)

    earnings, next_cursor = await get_author_earnings_history(org_id, cursor=cursor_dt, limit=limit, tool_name=tool_name)
    return JSONResponse(
        content={
            "earnings": [
                {
                    "id": e.id,
                    "tool_name": e.tool_name,
                    "caller_org_id": e.caller_org_id,
                    "total_cost_usdc": e.amount_usdc,
                    "author_share_usdc": e.author_share_usdc,
                    "platform_share_usdc": e.platform_share_usdc,
                    "status": e.status,
                    "created_at": e.created_at.isoformat(),
                }
                for e in earnings
            ],
            "next_cursor": next_cursor,
        }
    )


class MarketplaceEarningsByToolEntry(BaseModel):
    tool_name: str
    total_calls: int
    total_amount_usdc: int
    total_author_share_usdc: int
    pending_author_share_usdc: int
    settled_author_share_usdc: int
    total_platform_share_usdc: int


class MarketplaceEarningsByToolResponse(BaseModel):
    tools: list[MarketplaceEarningsByToolEntry]


@router.get("/marketplace/earnings/by-tool", tags=["Marketplace"], response_model=MarketplaceEarningsByToolResponse)
async def get_marketplace_earnings_by_tool_endpoint(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return per-tool earnings aggregates for the authenticated org."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    tools = await get_author_earnings_by_tool(org_id)
    return JSONResponse(
        content={
            "tools": [
                {
                    "tool_name": tool.tool_name,
                    "total_calls": tool.total_calls,
                    "total_amount_usdc": tool.total_amount_usdc,
                    "total_author_share_usdc": tool.total_author_share_usdc,
                    "pending_author_share_usdc": tool.pending_author_share_usdc,
                    "settled_author_share_usdc": tool.settled_author_share_usdc,
                    "total_platform_share_usdc": tool.total_platform_share_usdc,
                }
                for tool in tools
            ]
        }
        # ─── Author Withdrawals (on-chain USDC payout to settlement wallet) ────────
    )


class WithdrawRequest(BaseModel):
    amount_usdc: int = Field(..., gt=0)


class MarketplaceWithdrawalResponse(BaseModel):
    id: str
    org_id: str
    amount_usdc: int
    wallet: str
    status: str
    created_at: str = Field(..., description="ISO 8601 timestamp.")


@router.post(
    "/marketplace/withdraw",
    tags=["Marketplace"],
    response_model=MarketplaceWithdrawalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def request_marketplace_withdrawal(
    body: WithdrawRequest,
    payload: dict = Depends(require_org_admin),
) -> JSONResponse:
    """Request a withdrawal of earnings to the settlement wallet.

    Admin-only: moving funds out of the org balance is a financial control and
    must not be available to ordinary members.
    """
    org_id = _require_org_id(payload)

    try:
        withdrawal = await request_withdrawal(org_id, body.amount_usdc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    logger.info(
        "marketplace_withdrawal_requested org=%s by=%s id=%s amount_usdc=%s",
        org_id,
        payload["sub"],
        withdrawal.id,
        withdrawal.amount_usdc,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": withdrawal.id,
            "org_id": withdrawal.org_id,
            "amount_usdc": withdrawal.amount_usdc,
            "wallet": withdrawal.wallet,
            "status": withdrawal.status,
            "created_at": withdrawal.created_at.isoformat(),
        },
    )


class MarketplaceWithdrawalHistoryItem(BaseModel):
    id: str
    amount_usdc: int
    wallet: str
    tx_hash: str | None = None
    status: str
    created_at: str = Field(..., description="ISO 8601 timestamp.")
    settled_at: str | None = Field(default=None, description="ISO 8601 timestamp; null until settled.")


class MarketplaceWithdrawalsListResponse(BaseModel):
    withdrawals: list[MarketplaceWithdrawalHistoryItem]
    next_cursor: str | None = None


@router.get("/marketplace/withdrawals", tags=["Marketplace"], response_model=MarketplaceWithdrawalsListResponse)
async def get_marketplace_withdrawals(
    payload: dict = Depends(require_auth),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    """Get paginated withdrawal history (all statuses) for the authenticated org."""
    from marketplace import list_org_withdrawals
    from shared.pagination import parse_cursor

    org_id = _require_org_id(payload)
    cursor_dt = parse_cursor(cursor)

    withdrawals, next_cursor = await list_org_withdrawals(org_id, limit=limit, cursor=cursor_dt)
    return JSONResponse(
        content={
            "withdrawals": [
                {
                    "id": w.id,
                    "amount_usdc": w.amount_usdc,
                    "wallet": w.wallet,
                    "tx_hash": w.tx_hash,
                    "status": w.status,
                    "created_at": w.created_at.isoformat(),
                    "settled_at": w.settled_at.isoformat() if w.settled_at else None,
                }
                for w in withdrawals
            ],
            "next_cursor": next_cursor,
        }
    )


_CATALOG_VALID_SORTS = frozenset({"name", "price_asc", "price_desc", "popularity", "reputation"})
_AGENT_DIRECTORY_VALID_SORTS = frozenset({"name", "reputation"})
_AGENT_DIRECTORY_VALID_STALE_FILTERS = frozenset({"all", "active", "stale"})


class MarketplaceToolSummary(BaseModel):
    name: str
    qualified_name: str
    tool_name: str
    display_name: str
    description: str = Field(..., description="Marketplace-facing description.")
    short_description: str = Field(..., description="Internal/short description.")
    input_schema: dict[str, Any]
    cost_usdc: int
    tool_type: str
    category: str
    total_calls: int
    reputation_score: float
    success_rate: float
    unique_caller_count: int | None = None
    health_status: str
    is_healthy: bool
    author: str = Field(..., description="Display name; kept for backward compatibility.")
    author_slug: str = Field(..., description="Canonical author org slug filter key.")


class MarketplaceCatalogResponse(BaseModel):
    tools: list[MarketplaceToolSummary]
    next_cursor: str | None = None


class MarketplaceCatalogDetailResponse(BaseModel):
    tool: MarketplaceToolSummary


class MarketplaceQuoteResponse(BaseModel):
    qualified_name: str
    price_usdc: int = Field(..., ge=0, le=100_000_000, description="Current price in atomic USDC.")
    currency: Literal["USDC"] = "USDC"
    source: Literal["override", "marketplace"]
    expires_at: str = Field(..., description="ISO 8601 advisory expiry matching the active pricing-cache TTL.")


class MarketplaceDelegationQuoteResponse(BaseModel):
    max_cost_usdc: int = Field(..., ge=0, le=100_000_000, description="Global per-delegation cost cap in atomic USDC.")
    platform_fee_bps: int = Field(..., ge=0, description="Platform fee on delegations in basis points.")
    effective_max_charge_usdc: int = Field(..., ge=0, le=100_000_000, description="Cap plus platform fee, in atomic USDC.")
    currency: Literal["USDC"] = "USDC"
    expires_at: str = Field(..., description="ISO 8601 advisory expiry matching the active pricing-cache TTL.")


class MarketplaceAuthorSummary(BaseModel):
    org_slug: str
    org_name: str
    tool_count: int
    total_calls: int


class MarketplaceAuthorIndexResponse(BaseModel):
    authors: list[MarketplaceAuthorSummary]
    next_cursor: str | None = None


class MarketplaceAgentRegistrationRequest(BaseModel):
    agent_url: str = Field(..., min_length=1, max_length=2048)


class MarketplaceAgentRegistrationResponse(BaseModel):
    org_id: str
    agent_url: str
    created_at: str
    updated_at: str


class MarketplaceAgentSummary(BaseModel):
    org_slug: str
    org_name: str
    agent_url: str
    agent_card_url: str
    message_endpoint: str
    catalog_endpoint: str
    tool_count: int
    reputation_score: float | None = None
    success_rate: float | None = None
    sample_size: float | None = None
    confidence: float | None = None
    unique_caller_count: int | None = None
    last_event_at: str | None = None
    is_stale: bool | None = None


class MarketplaceAgentDirectoryResponse(BaseModel):
    agents: list[MarketplaceAgentSummary]
    next_cursor: str | None = None


@router.put(
    "/marketplace/agent-registration",
    tags=["Marketplace"],
    response_model=MarketplaceAgentRegistrationResponse,
)
async def set_marketplace_agent_registration(
    body: MarketplaceAgentRegistrationRequest,
    payload: dict = Depends(require_org_machine),
) -> JSONResponse:
    """Publish the authenticated organization's A2A endpoint."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    await _enforce_rate_limit(
        f"a2a_registration:{org_id}",
        s.rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration.",
    )
    try:
        registration = await set_agent_registration(org_id, body.agent_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None

    return JSONResponse(
        content={
            "org_id": registration["org_id"],
            "agent_url": registration["agent_url"],
            "created_at": registration["created_at"].isoformat(),
            "updated_at": registration["updated_at"].isoformat(),
        }
    )


@router.get(
    "/marketplace/agent-registration",
    tags=["Marketplace"],
    response_model=MarketplaceAgentRegistrationResponse,
)
async def get_marketplace_agent_registration(payload: dict = Depends(require_auth)) -> JSONResponse:
    """Return the authenticated organization's A2A endpoint registration."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    registration = await get_agent_registration(_require_org_id(payload))
    if registration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent registration not found.")
    return JSONResponse(
        content={
            "org_id": registration["org_id"],
            "agent_url": registration["agent_url"],
            "created_at": registration["created_at"].isoformat(),
            "updated_at": registration["updated_at"].isoformat(),
        }
    )


@router.delete("/marketplace/agent-registration", tags=["Marketplace"], status_code=status.HTTP_204_NO_CONTENT)
async def delete_marketplace_agent_registration(payload: dict = Depends(require_org_machine)) -> Response:
    """Unpublish the authenticated organization's A2A endpoint."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    await _enforce_rate_limit(
        f"a2a_registration:{org_id}",
        s.rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration.",
    )
    await delete_agent_registration(org_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/marketplace/agents",
    tags=["Marketplace"],
    response_model=MarketplaceAgentDirectoryResponse,
)
async def list_marketplace_agents_endpoint(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    sort: str = "name",
    stale: str = "all",
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
) -> JSONResponse:
    """Publicly list opt-in A2A endpoints and derived trust metrics.

    ``sort`` accepts ``name`` or ``reputation``; ``stale`` accepts ``all``,
    ``active``, or ``stale``. Privacy-suppressed or untested agents have
    ``is_stale=None`` and are returned only by ``stale=all``. Cursors are
    scoped to both query modes.
    """
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    if sort not in _AGENT_DIRECTORY_VALID_SORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid agent directory sort. Allowed: name, reputation.",
        )
    if stale not in _AGENT_DIRECTORY_VALID_STALE_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid agent directory stale filter. Allowed: active, all, stale.",
        )

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    cursor_data = _decode_agent_cursor(cursor)
    if cursor and cursor_data is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid agent directory cursor.")
    if cursor_data is not None and cursor_data[0] != sort:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Agent directory cursor does not match the requested sort.",
        )
    if cursor_data is not None and cursor_data[3] != stale:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Agent directory cursor does not match the requested stale filter.",
        )
    cursor_key: Any = None
    cursor_slug: str | None = None
    if cursor_data is not None:
        _, cursor_key, cursor_slug, _ = cursor_data

    search = q.strip().casefold() if q else ""
    snapshot = await get_agent_directory()
    candidates: list[dict[str, Any]] = []
    for agent in snapshot.get("agents", []):
        if not isinstance(agent, dict):
            continue
        org_slug = str(agent.get("org_slug", ""))
        org_name = str(agent.get("org_name", ""))
        agent_is_stale = agent.get("is_stale")
        if stale != "all" and agent_is_stale != (stale == "stale"):
            continue
        if search and search not in org_slug.casefold() and search not in org_name.casefold():
            continue
        agent_url = str(agent["agent_url"])
        candidates.append(
            {
                **agent,
                "agent_card_url": f"{agent_url}/.well-known/agent-card.json",
                "message_endpoint": f"{agent_url}/message:send",
                "catalog_endpoint": f"/marketplace/catalog?org_slug={org_slug}",
                "last_event_at": agent.get("last_event_at"),
                "is_stale": agent_is_stale,
            }
        )

    def reputation_score(agent: dict[str, Any]) -> float | None:
        value = agent.get("reputation_score")
        if value is None or isinstance(value, bool):
            return None
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return score if math.isfinite(score) else None

    if sort == "reputation":

        def reputation_sort_key(agent: dict[str, Any]) -> tuple[bool, float, str]:
            score = reputation_score(agent)
            return (score is None, -(score or 0.0), str(agent["org_slug"]))

        candidates.sort(key=reputation_sort_key)
    else:
        candidates.sort(key=lambda agent: str(agent["org_slug"]))

    if cursor_data is not None and cursor_slug is not None:
        if sort == "name":
            candidates = [agent for agent in candidates if str(agent["org_slug"]) > cursor_slug]
        else:
            cursor_score = float(cursor_key) if cursor_key is not None else None
            filtered_candidates: list[dict[str, Any]] = []
            for agent in candidates:
                agent_slug = str(agent["org_slug"])
                agent_score = reputation_score(agent)
                if cursor_score is None:
                    is_after = agent_score is None and agent_slug > cursor_slug
                else:
                    is_after = (
                        agent_score is None
                        or agent_score < cursor_score
                        or (agent_score == cursor_score and agent_slug > cursor_slug)
                    )
                if is_after:
                    filtered_candidates.append(agent)
            candidates = filtered_candidates

    page = candidates[:limit]
    next_cursor = _build_agent_cursor(page[-1], sort, stale) if len(page) == limit else None
    return JSONResponse(
        content={"agents": page, "next_cursor": next_cursor},
        headers={"Cache-Control": "public, max-age=60"},
    )


def _serialize_marketplace_tool(tool: Any) -> dict[str, Any]:
    result = {
        "name": tool.qualified_name,
        "qualified_name": tool.qualified_name,
        "tool_name": tool.name,
        "display_name": tool.display_name,
        "description": tool.marketplace_description,
        "short_description": tool.description,
        "input_schema": tool.input_schema,
        "cost_usdc": tool.cost_usdc,
        "tool_type": tool.tool_type,
        "category": tool.category,
        "total_calls": tool.total_calls,
        "reputation_score": tool.reputation_score,
        "success_rate": tool.success_rate,
        "health_status": tool.health_status,
        "is_healthy": tool.is_healthy,
        # author_slug is the canonical filter key; author is kept for
        # backward compatibility and human display.
        "author": tool.author_org_name,
        "author_slug": tool.author_org_slug,
    }
    if tool.unique_caller_count is not None:
        result["unique_caller_count"] = tool.unique_caller_count
    return result


def _format_atomic_usdc(amount_usdc: int) -> str:
    whole, fractional = divmod(max(0, int(amount_usdc)), 1_000_000)
    return f"${whole}.{fractional:06d}"


def _escape_llms_text(value: Any) -> str:
    # ─── Public Catalog & Author Discovery (browse/search marketplace) ─────────

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


@router.get("/marketplace/catalog", tags=["Marketplace"], response_model=MarketplaceCatalogResponse)
async def get_marketplace_catalog_endpoint(
    request: Request,
    org_slug: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=32),
    sort: str = "name",
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = None,
) -> JSONResponse:
    """Public: browse available marketplace tools with pricing.

    Query parameters:
    - **org_slug**: Filter to a single author org (use ``"platform"`` for
      Teardrop-owned tools). Omit for all tools.
        - **q**: Optional case-insensitive partial search across tool names,
            descriptions, and author fields.
        - **category**: Optional category filter (``defi``, ``search``, ``data``,
            ``communication``, or ``utility``).
        - **sort**: ``name`` (default), ``price_asc``, ``price_desc``, or
            ``popularity``.
    - **limit**: Maximum results to return (1–200, default 100).
    - **cursor**: Pagination token from a previous response's ``next_cursor``
      field. Omit for the first page.
    """
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    if sort not in _CATALOG_VALID_SORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid sort '{sort}'. Allowed: {', '.join(sorted(_CATALOG_VALID_SORTS))}",
        )
    if category is not None and category not in _MARKETPLACE_VALID_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid category '{category}'. Allowed: {', '.join(sorted(_MARKETPLACE_VALID_CATEGORIES))}",
        )

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    from marketplace import _build_catalog_cursor

    overrides = await get_tool_pricing_overrides()
    pricing = await get_current_pricing()
    default_cost = pricing.tool_call_cost if pricing else 0

    catalog = await get_marketplace_catalog(
        overrides,
        default_cost,
        org_slug=org_slug,
        q=q,
        category=category,
        sort=sort,
        limit=limit,
        cursor=cursor,
    )

    # Build next_cursor from the last item so callers can paginate.
    next_cursor: str | None = None
    if len(catalog) == limit:
        next_cursor = _build_catalog_cursor(catalog[-1], sort)

    return JSONResponse(
        content={
            "tools": [_serialize_marketplace_tool(t) for t in catalog],
            "next_cursor": next_cursor,
        },
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/marketplace/quote", tags=["Marketplace"], response_model=MarketplaceQuoteResponse)
async def get_marketplace_quote(
    request: Request,
    tool: str = Query(..., min_length=3, max_length=128),
) -> JSONResponse:
    """Public: quote the current effective price for one published marketplace tool."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    if tool.count("/") != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tool must use the qualified '{org_slug}/{tool_name}' form.",
        )
    org_slug, tool_name = tool.split("/", 1)
    if not org_slug or not tool_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tool must use the qualified '{org_slug}/{tool_name}' form.",
        )

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    overrides = await get_tool_pricing_overrides()
    pricing = await get_live_pricing()
    default_cost = pricing.tool_call_cost if pricing else 0
    catalog_tool = await get_marketplace_catalog_tool(tool_name, org_slug, overrides, default_cost)
    if catalog_tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace tool not found.")

    resolver_name = tool_name if org_slug == "platform" else tool
    price_usdc = await resolve_tool_cost(resolver_name, overrides, default_cost, marketplace_enabled=True)
    override_name = resolver_name if resolver_name in overrides else tool_name if tool_name in overrides else None
    source = "override" if override_name is not None else "marketplace"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=s.pricing_cache_ttl_seconds)

    return JSONResponse(
        content={
            "qualified_name": tool,
            "price_usdc": price_usdc,
            "currency": "USDC",
            "source": source,
            "expires_at": expires_at.isoformat(),
        },
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/marketplace/delegation/quote", tags=["Marketplace"], response_model=MarketplaceDelegationQuoteResponse)
async def get_marketplace_delegation_quote(request: Request) -> JSONResponse:
    """Public: quote the deterministic default per-delegation charge (global cap plus platform fee)."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    effective_max_charge_usdc = apply_platform_fee(s.a2a_delegation_max_cost_usdc)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=s.pricing_cache_ttl_seconds)

    return JSONResponse(
        content={
            "max_cost_usdc": s.a2a_delegation_max_cost_usdc,
            "platform_fee_bps": s.a2a_delegation_platform_fee_bps,
            "effective_max_charge_usdc": effective_max_charge_usdc,
            "currency": "USDC",
            "expires_at": expires_at.isoformat(),
        },
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/marketplace/authors", tags=["Marketplace"], response_model=MarketplaceAuthorIndexResponse)
async def list_marketplace_authors_endpoint(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
) -> JSONResponse:
    """Public: list marketplace authors grouped above their published tools."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    authors = await list_marketplace_authors_data(q=q, limit=limit, cursor=cursor)
    next_cursor: str | None = None
    if len(authors) == limit:
        from marketplace import _build_author_cursor

        next_cursor = _build_author_cursor(authors[-1])

    return JSONResponse(
        content={
            "authors": [MarketplaceAuthorSummary(**author).model_dump() for author in authors],
            "next_cursor": next_cursor,
        },
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get(
    "/marketplace/catalog/{org_slug}/{tool_name}",
    tags=["Marketplace"],
    response_model=MarketplaceCatalogDetailResponse,
)
async def get_marketplace_catalog_detail(
    request: Request,
    org_slug: str,
    tool_name: str,
) -> JSONResponse:
    """Public: return one marketplace catalog tool by qualified name parts."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    overrides = await get_tool_pricing_overrides()
    pricing = await get_current_pricing()
    default_cost = pricing.tool_call_cost if pricing else 0
    tool = await get_marketplace_catalog_tool(tool_name, org_slug, overrides, default_cost)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace tool not found.")

    return JSONResponse(content={"tool": _serialize_marketplace_tool(tool)}, headers={"Cache-Control": "public, max-age=60"})


class RunFeedbackRequest(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=128)
    rating: int = Field(..., ge=-1, le=1, description="-1 (bad), 0 (neutral), or 1 (good)")
    comment: str = Field(default="", max_length=1000)


class RunFeedbackResponse(BaseModel):
    id: str = Field(..., description="Feedback record ID.")
    run_id: str
    qualified_tool_name: str = Field(..., description="'{org_slug}/{tool_name}' the feedback applies to.")
    rating: int = Field(..., ge=-1, le=1)
    created_at: str = Field(..., description="ISO 8601 creation timestamp.")


@router.post(
    "/marketplace/tools/{org_slug}/{tool_name}/feedback",
    tags=["Marketplace"],
    response_model=RunFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_marketplace_tool_feedback(
    org_slug: str,
    tool_name: str,
    body: RunFeedbackRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Submit a ground-truth quality signal (-1/0/1) for a tool call within a run.

    Scoped to the authenticated user's own run: ``get_invoice_by_run`` must
    confirm the run belongs to the caller before feedback is accepted, so a
    caller cannot submit feedback for runs they never made. This is the first
    labeled signal available for future ML quality/reputation classifiers.
    """
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    user_id = payload["sub"]
    org_id = _require_org_id(payload)

    invoice = await get_invoice_by_run(body.run_id, user_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found for this account.")

    feedback = await record_run_feedback(
        run_id=body.run_id,
        org_id=org_id,
        user_id=user_id,
        qualified_tool_name=f"{org_slug}/{tool_name}",
        rating=body.rating,
        comment=body.comment,
    )

    # Best-effort: attach this rating to the run's decision-graph record too
    # (if one exists and hasn't already been labeled). Never blocks or fails
    # the feedback submission — decision-graph backfill is non-critical.
    try:
        from teardrop.memory import backfill_decision_outcome  # noqa: PLC0415

        await backfill_decision_outcome(body.run_id, org_id, body.rating, source="feedback")
    except Exception:
        logger.debug("Decision outcome backfill failed for run_id=%s", body.run_id, exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": feedback["id"],
            "run_id": feedback["run_id"],
            "qualified_tool_name": feedback["qualified_tool_name"],
            "rating": feedback["rating"],
            "created_at": feedback["created_at"].isoformat(),
        },
    )


class MarketplaceAuthorProfileResponse(BaseModel):
    org_slug: str
    org_name: str
    tool_count: int
    total_calls: int
    tools: list[MarketplaceToolSummary]
    next_cursor: str | None = None


@router.get("/marketplace/authors/{org_slug}", tags=["Marketplace"], response_model=MarketplaceAuthorProfileResponse)
async def get_marketplace_author_profile(
    request: Request,
    org_slug: str,
    sort: str = "popularity",
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> JSONResponse:
    """Public: return marketplace author metadata and published tools."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")
    if sort not in _CATALOG_VALID_SORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid sort '{sort}'. Allowed: {', '.join(sorted(_CATALOG_VALID_SORTS))}",
        )

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    summary = await get_marketplace_author_summary(org_slug)
    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace author not found.")

    from marketplace import _build_catalog_cursor

    overrides = await get_tool_pricing_overrides()
    pricing = await get_current_pricing()
    default_cost = pricing.tool_call_cost if pricing else 0
    catalog = await get_marketplace_catalog(
        overrides,
        default_cost,
        org_slug=org_slug,
        sort=sort,
        limit=limit,
        cursor=cursor,
    )

    next_cursor: str | None = None
    if len(catalog) == limit:
        next_cursor = _build_catalog_cursor(catalog[-1], sort)

    return JSONResponse(
        content={
            **summary,
            "tools": [_serialize_marketplace_tool(t) for t in catalog],
            "next_cursor": next_cursor,
        },
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/marketplace/llms.txt", include_in_schema=False)
async def marketplace_llms_txt(request: Request) -> Response:
    """Public: LLM-friendly marketplace catalog index."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    from marketplace import _build_catalog_cursor

    overrides = await get_tool_pricing_overrides()
    pricing = await get_current_pricing()
    default_cost = pricing.tool_call_cost if pricing else 0
    base_url = str(request.base_url).rstrip("/")
    lines = [
        "# Teardrop Marketplace",
        "",
        "Public MCP tools available through Teardrop.",
        "",
        "Each tool lists its purpose, price, health, and a link to its detail page and",
        "aggregate reputation. Agents should read the description before choosing a tool.",
        "",
    ]

    cursor: str | None = None
    seen = 0
    while True:
        catalog = await get_marketplace_catalog(
            overrides,
            default_cost,
            sort="name",
            limit=200,
            cursor=cursor,
        )
        if not catalog:
            break
        for tool in catalog:
            seen += 1
            detail_url = f"{base_url}/marketplace/catalog/{tool.author_org_slug}/{tool.name}"
            quote_url = f"{base_url}/marketplace/quote?tool={tool.qualified_name}"
            description = tool.marketplace_description or tool.description
            lines.append(
                f"## {_escape_llms_text(tool.qualified_name)}\n"
                f"- Description: {_escape_llms_text(description)}\n"
                f"- Author: {_escape_llms_text(tool.author_org_name)}\n"
                f"- Category: {_escape_llms_text(tool.category or 'uncategorized')}\n"
                f"- Health: {_escape_llms_text(tool.health_status)}\n"
                f"- Calls: {tool.total_calls}\n"
                f"- Price: {_format_atomic_usdc(tool.cost_usdc)}\n"
                f"- [Detail]({detail_url})\n"
                f"- [Quote]({quote_url})\n"
                f"- [Reputation]({base_url}/.well-known/reputation.json)\n"
            )
        if len(catalog) < 200 or seen >= 10_000:
            break
        cursor = _build_catalog_cursor(catalog[-1], "name")

    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ─── Marketplace Subscriptions ────────────────────────────────────────────────


class SubscribeRequest(BaseModel):
    qualified_tool_name: str = Field(..., min_length=3, max_length=128, pattern=r"^[a-z0-9_-]+/[a-z0-9_]+$")


class MarketplaceSubscriptionResponse(BaseModel):
    id: str
    org_id: str
    qualified_tool_name: str
    is_active: bool
    subscribed_at: str = Field(..., description="ISO 8601 timestamp.")


@router.post(
    "/marketplace/subscriptions",
    tags=["Marketplace"],
    response_model=MarketplaceSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def subscribe_to_marketplace_tool(
    body: SubscribeRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Subscribe the authenticated org to a marketplace tool for /agent/run injection."""
    from marketplace import PlatformToolSubscriptionError, SelfSubscribeError, subscribe_to_tool

    org_id: str = payload.get("org_id", "")
    try:
        sub = await subscribe_to_tool(org_id, body.qualified_tool_name)
    except PlatformToolSubscriptionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except SelfSubscribeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": sub.id,
            "org_id": sub.org_id,
            "qualified_tool_name": sub.qualified_tool_name,
            "is_active": sub.is_active,
            "subscribed_at": sub.subscribed_at.isoformat(),
        },
    )


class MarketplaceSubscriptionItem(BaseModel):
    id: str
    qualified_tool_name: str
    subscribed_at: str = Field(..., description="ISO 8601 timestamp.")


class MarketplaceSubscriptionListResponse(BaseModel):
    subscriptions: list[MarketplaceSubscriptionItem]


@router.get("/marketplace/subscriptions", tags=["Marketplace"], response_model=MarketplaceSubscriptionListResponse)
async def list_marketplace_subscriptions(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List active marketplace subscriptions for the authenticated org."""
    from marketplace import get_org_subscriptions

    org_id: str = payload.get("org_id", "")
    subs = await get_org_subscriptions(org_id)
    return JSONResponse(
        content={
            "subscriptions": [
                {
                    "id": s.id,
                    "qualified_tool_name": s.qualified_tool_name,
                    "subscribed_at": s.subscribed_at.isoformat(),
                }
                for s in subs
            ]
        }
    )


class UnsubscribeResponse(BaseModel):
    unsubscribed: Literal[True]


@router.delete("/marketplace/subscriptions/{subscription_id}", tags=["Marketplace"], response_model=UnsubscribeResponse)
async def unsubscribe_from_marketplace_tool(
    subscription_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Unsubscribe from a marketplace tool."""
    from marketplace import unsubscribe_from_tool

    org_id: str = payload.get("org_id", "")
    ok = await unsubscribe_from_tool(subscription_id, org_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found.")
    return JSONResponse(content={"unsubscribed": True})
