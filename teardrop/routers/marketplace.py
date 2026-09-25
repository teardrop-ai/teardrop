# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace REST routes: author config/earnings/withdrawals, public catalog
browsing, and subscriptions.

Sub-domains (each with its own section below):
  1. Author Config          — set/get settlement wallet for payouts
  2. Author Earnings        — balance, history, per-tool aggregates (atomic USDC)
  3. Author Withdrawals     — request payout to settlement wallet
  4. Public Catalog         — browse/search marketplace, author discovery
  5. Subscriptions          — subscribe/unsubscribe to published tools

Extracted verbatim from ``teardrop.app`` with no logic changes. Billing, x402,
SSRF, circuit-breaker, and subscription-gate semantics are preserved exactly.
Sibling marketplace routers:
  * ``teardrop.routers.marketplace_import`` — MCP import preview/publish
  * ``teardrop.routers.marketplace_agents`` — A2A agent registration + directory
  * ``teardrop.routers.marketplace_mcp``    — MCP JSON-RPC gateway (POST /mcp/v1)
"""

from __future__ import annotations

import logging
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
    get_author_balance,
    get_author_config,
    get_author_earnings_by_tool,
    get_author_earnings_history,
    get_marketplace_author_summary,
    get_marketplace_catalog,
    get_marketplace_catalog_tool,
    record_run_feedback,
    request_withdrawal,
    set_author_config,
)
from marketplace import list_marketplace_authors as list_marketplace_authors_data
from teardrop.config import get_settings
from teardrop.dependencies import (
    _require_org_id,
    require_auth,
    require_settlement_wallet_auth,
)
from teardrop.funnel_counters import SURFACE_CATALOG, SURFACE_QUOTE, record_discovery_hit
from teardrop.rate_limit import _enforce_rate_limit

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

_MARKETPLACE_VALID_CATEGORIES = {"", "defi", "search", "data", "communication", "utility"}


# ─── MCP Marketplace – REST API ──────────────────────────────────────────────


class SetAuthorConfigRequest(BaseModel):
    settlement_wallet: str = Field(..., min_length=42, max_length=42)


class MarketplaceAuthorConfigResponse(BaseModel):
    org_id: str
    settlement_wallet: str | None = None
    created_at: str | None = Field(default=None, description="ISO 8601 timestamp; null if unconfigured.")
    updated_at: str | None = Field(default=None, description="ISO 8601 timestamp; null if unconfigured.")


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
    )


# ─── Author Earnings & Balance (atomic USDC ledger) ──────────────────────


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
    payload: dict = Depends(require_settlement_wallet_auth),
) -> JSONResponse:
    """Admins or machine-org SIWE owners withdraw to the configured settlement wallet."""
    s = get_settings()
    org_id = _require_org_id(payload)

    await _enforce_rate_limit(
        f"marketplace:withdraw:{org_id}",
        s.rate_limit_auth_rpm,
        detail="Rate limit exceeded for withdrawal requests.",
    )

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

    record_discovery_hit(SURFACE_CATALOG)
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

    record_discovery_hit(SURFACE_QUOTE)
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
