# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""MCP marketplace routes: JSON-RPC gateway (/mcp/v1), author config/earnings/
withdrawals, public catalog browsing, and subscriptions.

Extracted verbatim from ``teardrop.app`` with no logic changes. Billing, x402,
SSRF, circuit-breaker, and subscription-gate semantics are preserved exactly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing import (
    BillingResult,
    debit_credit,
    get_current_pricing,
    get_tool_pricing_overrides,
    verify_credit,
)
from marketplace import (
    check_org_subscription,
    get_author_balance,
    get_author_config,
    get_author_earnings_by_tool,
    get_author_earnings_history,
    get_marketplace_author_summary,
    get_marketplace_catalog,
    get_marketplace_catalog_tool,
    get_marketplace_tool_by_name,
    record_marketplace_tool_usage_many,
    record_tool_call_earnings,
    request_withdrawal,
    set_author_config,
)
from teardrop._meta import APP_VERSION
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.rate_limit import _enforce_rate_limit
from tools import registry
from tools.executor import execute_tool

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

_MARKETPLACE_VALID_CATEGORIES = {"", "defi", "search", "data", "communication", "utility"}


# ─── MCP Marketplace – JSON-RPC Handler ─────────────────────────────────────


async def _execute_marketplace_tool(tool_row: dict[str, Any], arguments: dict[str, Any]) -> Any:
    """Execute a published marketplace tool via its webhook.

    ``tool_row`` is the raw DB row returned by ``get_marketplace_tool_by_name()``.
    Follows the same SSRF-safe webhook pattern as ``_build_langchain_tool``.
    """
    import time as _time  # noqa: PLC0415

    import aiohttp  # noqa: PLC0415

    from org_tools import _decrypt_header, _hash_webhook_host, _on_webhook_failure, _record_event  # noqa: PLC0415
    from tools.definitions.http_fetch import async_validate_url  # noqa: PLC0415
    from tools.health import is_breaker_tripped, record_success  # noqa: PLC0415

    tool_id = tool_row.get("id", "")
    org_id = tool_row.get("org_id", "")
    tool_name = tool_row.get("name", "")
    url = tool_row["webhook_url"]
    method = tool_row.get("webhook_method", "POST")
    timeout_sec = tool_row.get("timeout_seconds", 10)
    host_hash = _hash_webhook_host(url)

    if tool_id and await is_breaker_tripped(tool_id):
        return {"error": "Tool temporarily unavailable (circuit breaker tripped)"}

    url_err = await async_validate_url(url)
    if url_err:
        return {"error": f"Webhook URL blocked: {url_err}"}

    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth_name = tool_row.get("auth_header_name")
    auth_enc = tool_row.get("auth_header_enc")
    if auth_name and auth_enc:
        try:
            headers[auth_name] = _decrypt_header(auth_enc)
        except Exception:
            if tool_id:
                await _on_webhook_failure(tool_id, org_id, tool_name, host_hash, "decrypt_failure")
            return {"error": "Failed to decrypt webhook auth header"}

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    started = _time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if method == "GET":
                resp = await session.get(url, headers=headers, params=arguments)
            elif method == "PUT":
                resp = await session.put(url, headers=headers, json=arguments)
            else:
                resp = await session.post(url, headers=headers, json=arguments)

            body = await resp.read()
            # 512 KB response cap
            if len(body) > 512 * 1024:
                body = body[: 512 * 1024]

            content_type = resp.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                if tool_id:
                    await _on_webhook_failure(
                        tool_id,
                        org_id,
                        tool_name,
                        host_hash,
                        "non_json_response",
                        status_code=resp.status,
                    )
                return {"text": body.decode("utf-8", errors="replace")}

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                if tool_id:
                    await _on_webhook_failure(
                        tool_id,
                        org_id,
                        tool_name,
                        host_hash,
                        "invalid_json",
                        status_code=resp.status,
                    )
                return {"error": "Webhook returned invalid JSON"}

            if resp.status >= 400:
                if tool_id:
                    await _on_webhook_failure(
                        tool_id,
                        org_id,
                        tool_name,
                        host_hash,
                        "http_error",
                        status_code=resp.status,
                    )
                return {"error": f"Webhook returned HTTP {resp.status}", "status": resp.status}

            # Success.
            if tool_id:
                latency_ms = int((_time.monotonic() - started) * 1000)
                await record_success(tool_id)
                await _record_event(
                    org_id,
                    tool_id,
                    tool_name,
                    "executed",
                    actor_id="mcp",
                    detail={"latency_ms": latency_ms, "status": resp.status},
                )
            return payload
    except asyncio.TimeoutError:
        if tool_id:
            await _on_webhook_failure(tool_id, org_id, tool_name, host_hash, "timeout")
        return {"error": f"Webhook timed out after {timeout_sec}s"}
    except Exception as exc:
        if tool_id:
            await _on_webhook_failure(tool_id, org_id, tool_name, host_hash, type(exc).__name__)
        return {"error": f"Webhook request failed: {type(exc).__name__}"}


def _jsonrpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _jsonrpc_result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


@router.post("/mcp/v1", tags=["MCP Marketplace"])
async def mcp_jsonrpc_handler(
    request: Request,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """MCP Streamable HTTP endpoint — JSON-RPC 2.0.

    Implements the following MCP methods:
      - ``initialize`` – returns server capabilities
      - ``tools/list`` – returns available tool definitions with pricing
      - ``tools/call`` – execute a tool (subject to billing gate)
    """
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP marketplace is not enabled.",
        )

    user_id: str = payload["sub"]
    org_id: str = payload.get("org_id", "")

    # Rate limit (separate MCP bucket)
    await _enforce_rate_limit(
        f"mcp:{user_id}",
        s.rate_limit_mcp_rpm,
        detail="MCP rate limit exceeded.",
    )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content=_jsonrpc_error(None, -32700, "Parse error"),
            status_code=200,
        )

    req_id = body.get("id")
    method = body.get("method", "")

    if body.get("jsonrpc") != "2.0":
        return JSONResponse(content=_jsonrpc_error(req_id, -32600, "Invalid JSON-RPC version"))

    # ── initialize ────────────────────────────────────────────────────────
    if method == "initialize":
        return JSONResponse(
            content=_jsonrpc_result(
                req_id,
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "teardrop-marketplace", "version": APP_VERSION},
                },
            )
        )

    # ── tools/list ────────────────────────────────────────────────────────
    if method == "tools/list":
        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0

        catalog = await get_marketplace_catalog(overrides, default_cost)

        tools_list = [
            {
                "name": t.qualified_name,
                "description": t.marketplace_description,
                "inputSchema": t.input_schema,
            }
            for t in catalog
        ]

        # Include built-in tools as well
        for bt in registry.list_latest():
            tools_list.append(
                {
                    "name": bt.name,
                    "description": bt.description,
                    "inputSchema": bt.input_schema.model_json_schema(),
                }
            )

        return JSONResponse(content=_jsonrpc_result(req_id, {"tools": tools_list}))

    # ── tools/call ────────────────────────────────────────────────────────
    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if not tool_name:
            return JSONResponse(content=_jsonrpc_error(req_id, -32602, "Missing tool name"))

        # Determine tool cost
        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0

        # Check if it's a marketplace tool (qualified_name = "org_slug/tool_name")
        is_marketplace_tool = "/" in tool_name
        if is_marketplace_tool:
            tool_org_slug, actual_tool_name = tool_name.split("/", 1)
        else:
            tool_org_slug, actual_tool_name = "", tool_name

        # Subscription gate: marketplace tools require an active subscription.
        if is_marketplace_tool:
            if not await check_org_subscription(org_id, tool_name):
                logger.info("mcp/v1 subscription check failed org_id=%s tool=%s", org_id, tool_name)
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        -32001,
                        f"Not subscribed to marketplace tool '{tool_name}'. Subscribe via POST /marketplace/subscriptions.",
                    )
                )

        # Price resolution: admin override (qualified) > admin override (bare) > author price > default
        tool_cost = overrides.get(tool_name, overrides.get(actual_tool_name, default_cost))

        # ── Billing gate (credit-only for MCP calls) ──────────────────
        billing = BillingResult()
        if s.billing_enabled:
            billing = await verify_credit(org_id, tool_cost)
            if not billing.verified:
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        -32000,
                        f"Insufficient credit balance. Required: {tool_cost} USDC atomic units.",
                    )
                )

        # ── Resolve and execute tool ──────────────────────────────────
        result: Any
        author_org_id: str | None = None

        if is_marketplace_tool:
            tool_row = await get_marketplace_tool_by_name(actual_tool_name, tool_org_slug)
            if tool_row is None:
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"),
                )
            author_org_id = tool_row.get("org_id")
            # Refine cost with author base_price_usdc if no admin override exists
            author_price = tool_row.get("base_price_usdc", 0)
            if tool_name not in overrides and actual_tool_name not in overrides and author_price:
                tool_cost = author_price
            result = await _execute_marketplace_tool(tool_row, arguments)
        else:
            # Built-in tool execution
            tool_def = registry.get(tool_name)
            if tool_def is None:
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"),
                )
            exec_result = await execute_tool(
                tool_name=tool_name,
                tool_call_id=str(req_id),
                tool_args=arguments,
                invoke=tool_def.implementation,
                timeout_seconds=tool_def.timeout_seconds,
                output_schema=tool_def.output_schema,
            )
            if exec_result.success:
                try:
                    result = json.loads(exec_result.content)
                except Exception:
                    result = exec_result.content
            else:
                try:
                    result = {"error": json.loads(exec_result.content).get("message", "Tool execution failed")}
                except Exception:
                    result = {"error": "Tool execution failed"}

        # ── Debit credits ─────────────────────────────────────────────
        # Skip debit when execution failed: subscribers must not be charged
        # for infrastructure failures, breaker trips, or auth/decryption errors.
        execution_failed = isinstance(result, dict) and "error" in result
        debited = False
        if billing.verified and billing.billing_method == "credit" and not execution_failed:
            debited, _ = await debit_credit(org_id, tool_cost, reason=f"mcp:{tool_name}")

        # ── Record author earnings (fire-and-forget) ──────────────────
        # Only record earnings when the caller was actually charged to prevent
        # phantom earnings entries when billing is disabled or debit failed.
        if author_org_id and tool_cost > 0 and debited:
            try:
                asyncio.create_task(
                    record_tool_call_earnings(
                        author_org_id=author_org_id,
                        caller_org_id=org_id,
                        tool_name=actual_tool_name,
                        total_cost_usdc=tool_cost,
                    )
                )
            except Exception:
                logger.debug("Failed to record author earnings", exc_info=True)

        if debited:
            try:
                asyncio.create_task(record_marketplace_tool_usage_many([tool_name]))
            except Exception:
                logger.debug("Failed to record marketplace tool stats", exc_info=True)

        # Format MCP-spec tool result
        if isinstance(result, dict) and "error" in result:
            return JSONResponse(
                content=_jsonrpc_result(
                    req_id,
                    {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                        "isError": True,
                    },
                )
            )

        return JSONResponse(
            content=_jsonrpc_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result) if not isinstance(result, str) else result,
                        }
                    ],
                    "isError": False,
                },
            )
        )

    # Unknown method
    return JSONResponse(content=_jsonrpc_error(req_id, -32601, f"Method not found: {method}"))


# ─── MCP Marketplace – REST API ──────────────────────────────────────────────


class SetAuthorConfigRequest(BaseModel):
    settlement_wallet: str = Field(..., min_length=42, max_length=42)


@router.post("/marketplace/author-config", tags=["Marketplace"])
async def set_marketplace_author_config(
    body: SetAuthorConfigRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Configure or update the marketplace author settings for the org."""
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
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Request a withdrawal of earnings to the settlement wallet."""
    org_id = _require_org_id(payload)

    try:
        withdrawal = await request_withdrawal(org_id, body.amount_usdc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

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
    category: str | None = Query(default=None, max_length=32),
    sort: str = "name",
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = None,
) -> JSONResponse:
    """Public: browse available marketplace tools with pricing.

    Query parameters:
    - **org_slug**: Filter to a single author org (use ``"platform"`` for
      Teardrop-owned tools). Omit for all tools.
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
