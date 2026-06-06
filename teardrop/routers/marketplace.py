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
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth, require_org_admin
from teardrop.rate_limit import _enforce_rate_limit

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

_MARKETPLACE_VALID_CATEGORIES = {"", "defi", "search", "data", "communication", "utility"}


# ─── MCP Marketplace – REST API ──────────────────────────────────────────────


class SetAuthorConfigRequest(BaseModel):
    settlement_wallet: str = Field(..., min_length=42, max_length=42)


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
    from marketplace import PlatformToolSubscriptionError, subscribe_to_tool

    org_id: str = payload.get("org_id", "")
    try:
        sub = await subscribe_to_tool(org_id, body.qualified_tool_name)
    except PlatformToolSubscriptionError as exc:
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
