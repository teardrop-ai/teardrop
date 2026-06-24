# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace REST routes: author config/earnings/withdrawals, public catalog
browsing, and subscriptions.

Extracted verbatim from ``teardrop.app`` with no logic changes. Billing, x402,
SSRF, circuit-breaker, and subscription-gate semantics are preserved exactly.
The MCP JSON-RPC gateway (POST /mcp/v1) lives in ``teardrop.routers.marketplace_mcp``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing import (
    get_current_pricing,
    get_tool_pricing_overrides,
)
from marketplace import (
    get_author_balance,
    get_author_config,
    get_author_earnings_by_tool,
    get_author_earnings_history,
    get_marketplace_author_summary,
    get_marketplace_catalog,
    get_marketplace_catalog_tool,
    request_withdrawal,
    set_author_config,
)
from marketplace.models import MarketplaceCategory
from mcp_client import discover_mcp_tools, get_org_mcp_server
from org_tools import create_org_tool, list_org_tools, validate_safe_schema_subset
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth, require_org_admin
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


@router.post("/marketplace/author-config", tags=["Marketplace"])
async def set_marketplace_author_config(
    body: SetAuthorConfigRequest,
    payload: dict = Depends(require_org_admin),
) -> JSONResponse:
    """Configure or update the marketplace author settings for the org.

    Admin-only: the settlement wallet is the destination for all marketplace
    payouts, so changing it is a financial control and must not be available to
    ordinary members.
    """
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)

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


@router.get("/marketplace/author-config", tags=["Marketplace"])
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
    )


@router.post("/marketplace/import/preview", tags=["Marketplace"])
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


@router.post("/marketplace/import/publish", tags=["Marketplace"])
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
    )


@router.get("/marketplace/balance", tags=["Marketplace"])
async def get_marketplace_balance(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get the pending (unwithdrawn) earnings balance for the authenticated org."""
    org_id = _require_org_id(payload)

    balance = await get_author_balance(org_id)
    return JSONResponse(content={"org_id": org_id, "balance_usdc": balance})


@router.get("/marketplace/earnings", tags=["Marketplace"])
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


@router.get("/marketplace/earnings/by-tool", tags=["Marketplace"])
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
    )


class WithdrawRequest(BaseModel):
    amount_usdc: int = Field(..., gt=0)


@router.post("/marketplace/withdraw", tags=["Marketplace"])
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


@router.get("/marketplace/withdrawals", tags=["Marketplace"])
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


_CATALOG_VALID_SORTS = frozenset({"name", "price_asc", "price_desc", "popularity"})


def _serialize_marketplace_tool(tool: Any) -> dict[str, Any]:
    return {
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
        "health_status": tool.health_status,
        "is_healthy": tool.is_healthy,
        # author_slug is the canonical filter key; author is kept for
        # backward compatibility and human display.
        "author": tool.author_org_name,
        "author_slug": tool.author_org_slug,
    }


def _format_atomic_usdc(amount_usdc: int) -> str:
    whole, fractional = divmod(max(0, int(amount_usdc)), 1_000_000)
    return f"${whole}.{fractional:06d}"


def _escape_llms_text(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").replace("|", "-").strip()


@router.get("/marketplace/catalog", tags=["Marketplace"])
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


@router.get("/marketplace/catalog/{org_slug}/{tool_name}", tags=["Marketplace"])
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


@router.get("/marketplace/authors/{org_slug}", tags=["Marketplace"])
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
        "| Tool | Author | Category | Health | Calls | Price | URL |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
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
            lines.append(
                "| "
                f"{_escape_llms_text(tool.qualified_name)} | "
                f"{_escape_llms_text(tool.author_org_name)} | "
                f"{_escape_llms_text(tool.category or 'uncategorized')} | "
                f"{_escape_llms_text(tool.health_status)} | "
                f"{tool.total_calls} | "
                f"{_format_atomic_usdc(tool.cost_usdc)} | "
                f"{detail_url} |"
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


@router.post("/marketplace/subscriptions", tags=["Marketplace"])
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


@router.get("/marketplace/subscriptions", tags=["Marketplace"])
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


@router.delete("/marketplace/subscriptions/{subscription_id}", tags=["Marketplace"])
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
