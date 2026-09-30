# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Deprecated MCP marketplace JSON-RPC gateway (POST /mcp/v1); sunsets 2026-10-29.

Superseded by the ``/tools/mcp`` gateway, which serves the same platform and
community tools. Billing is credit-only. Community tools need no subscription;
subscriptions only pin tools into ``/agent/run``.

Methods handled by ``mcp_jsonrpc_handler``:
  * ``initialize`` – server capabilities / protocol version
  * ``tools/list`` – marketplace catalog + built-in tools with ``_meta`` price
  * ``tools/call`` – self-call/promo guards → argument validation → optional
    caller price cap → credit billing gate → execute → debit → record author
    earnings + usage stats

Marketplace tool webhooks are invoked by ``marketplace.execution``, which
applies ``async_validate_url`` (SSRF guard) before every outbound request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from billing import (
    BillingResult,
    debit_credit,
    get_current_pricing,
    get_tool_pricing_overrides,
    is_promotional_credit,
    resolve_tool_cost,
    verify_credit,
)
from billing.charges import record_charge
from marketplace import (
    PLATFORM_SLUG,
    get_marketplace_catalog,
    get_marketplace_tool_by_name,
    record_marketplace_tool_usage_many,
    record_tool_call_earnings,
)
from marketplace.execution import execute_marketplace_tool as _execute_marketplace_tool
from teardrop._meta import APP_VERSION
from teardrop.config import get_settings
from teardrop.dependencies import require_auth
from teardrop.public_url import public_base_url
from teardrop.rate_limit import _enforce_rate_limit
from tools import registry
from tools.executor import execute_tool
from tools.registry import MCP_MAX_COST_META_KEY, format_mcp_quality_description
from tools.registry import build_mcp_tool_meta as _tool_meta
from tools.schema import build_pydantic_model

logger = logging.getLogger(__name__)

router = APIRouter()


def _jsonrpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _jsonrpc_result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


# RFC 9745 / RFC 8594 values for the /tools/mcp migration window.
_DEPRECATED_AT = "@1790640000"
_SUNSET_AT = "Thu, 29 Oct 2026 00:00:00 GMT"


@router.post("/mcp/v1", tags=["MCP Marketplace"], deprecated=True)
async def mcp_jsonrpc_handler(
    request: Request,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Deprecated MCP JSON-RPC 2.0 endpoint; use ``POST /tools/mcp``."""
    logger.info("mcp/v1 deprecated call org_id=%s", payload.get("org_id", ""))
    response = await _handle_mcp_jsonrpc(request, payload)
    response.headers["Deprecation"] = _DEPRECATED_AT
    response.headers["Sunset"] = _SUNSET_AT
    response.headers["Link"] = f'<{public_base_url(request, get_settings())}/tools/mcp>; rel="successor-version"'
    return response


async def _handle_mcp_jsonrpc(request: Request, payload: dict) -> JSONResponse:
    """Handle ``initialize``, ``tools/list`` (with ``_meta`` price) and ``tools/call``."""
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
    if org_id:
        # Shared bucket with the /tools/mcp gateway so multi-principal orgs cannot fan out.
        await _enforce_rate_limit(
            f"mcp:org:{org_id}",
            s.rate_limit_org_mcp_rpm,
            detail="Organization MCP rate limit exceeded.",
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

        catalog = await get_marketplace_catalog(overrides, default_cost, limit=200)

        # Structured reputation for programmatic clients. Degrades to no `_meta`
        # when the aggregate is unavailable; never blocks tool listing.
        try:
            from marketplace.reputation import get_public_reputation

            reputation = await get_public_reputation()
        except Exception:
            logger.warning("MCP: reputation unavailable for tools/list")
            reputation = {}

        tools_list = []
        for t in catalog:
            # Platform rows duplicate the bare-named registry entries appended below.
            if t.tool_type == "platform":
                continue
            metrics = reputation.get(t.qualified_name)
            tools_list.append(
                {
                    "name": t.qualified_name,
                    "description": format_mcp_quality_description(t.marketplace_description, metrics),
                    "inputSchema": t.input_schema,
                    "_meta": _tool_meta(metrics, t.cost_usdc),
                }
            )

        # Include built-in tools as well
        for bt in registry.list_latest():
            metrics = reputation.get(f"platform/{bt.name}")
            tools_list.append(
                {
                    "name": bt.name,
                    "description": format_mcp_quality_description(bt.description, metrics),
                    "inputSchema": bt.input_schema.model_json_schema(),
                    "_meta": _tool_meta(metrics, await resolve_tool_cost(bt.name, overrides, default_cost, True)),
                }
            )

        return JSONResponse(content=_jsonrpc_result(req_id, {"tools": tools_list}))

    # ── tools/call ────────────────────────────────────────────────────────
    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        # Catalog qualified names for platform tools map to the always-included built-in.
        tool_name = tool_name.removeprefix(f"{PLATFORM_SLUG}/") if isinstance(tool_name, str) else ""

        if not tool_name:
            return JSONResponse(content=_jsonrpc_error(req_id, -32602, "Missing tool name"))

        # Check if it's a marketplace tool (qualified_name = "org_slug/tool_name")
        is_marketplace_tool = "/" in tool_name
        if is_marketplace_tool:
            tool_org_slug, actual_tool_name = tool_name.split("/", 1)
        else:
            tool_org_slug, actual_tool_name = "", tool_name

        # Verified-email promotional credit cannot be used to generate
        # marketplace author earnings through this direct JSON-RPC route.
        if is_marketplace_tool and s.billing_enabled and s.onboarding_credit_enabled and await is_promotional_credit(org_id):
            logger.info("mcp/v1 promotional credit blocked marketplace tool org_id=%s tool=%s", org_id, tool_name)
            return JSONResponse(
                content=_jsonrpc_error(
                    req_id,
                    -32003,
                    "Marketplace author tools require a funded credit balance.",
                ),
                status_code=403,
            )

        result: Any
        author_org_id: str | None = None
        tool_row: dict | None = None

        if not isinstance(arguments, dict):
            return JSONResponse(
                content=_jsonrpc_error(req_id, -32602, f"Invalid arguments for tool '{tool_name}': expected an object"),
            )

        # ── Validate arguments BEFORE the billing gate so rejected calls are never charged ──
        if is_marketplace_tool:
            tool_row = await get_marketplace_tool_by_name(actual_tool_name, tool_org_slug)
            if tool_row is None:
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"),
                )
            author_org_id = tool_row.get("org_id")
            if author_org_id == org_id:
                # Self-calls would debit the author with zero earnings; testing has its own unbilled route.
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        -32005,
                        "Authors cannot call their own marketplace tool here; use POST /tools/test-webhook.",
                    ),
                )
            raw_schema = tool_row.get("input_schema") or {}
            if isinstance(raw_schema, str):
                raw_schema = json.loads(raw_schema)
            try:
                build_pydantic_model(
                    tool_name,
                    raw_schema,
                    model_name=f"MPTool_{tool_name.replace('/', '_')}_Input",
                )(**arguments)
            except PydanticValidationError:
                logger.info("mcp/v1 invalid arguments org_id=%s tool=%s", org_id, tool_name)
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32602, f"Invalid arguments for tool '{tool_name}'"),
                )
        else:
            # Built-in implementations bypass LangChain/Pydantic coercion, so validate explicitly.
            tool_def = registry.get(tool_name)
            if tool_def is None:
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"),
                )
            try:
                tool_def.input_schema(**arguments)
            except PydanticValidationError:
                logger.info("mcp/v1 invalid arguments org_id=%s tool=%s", org_id, tool_name)
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32602, f"Invalid arguments for tool '{tool_name}'"),
                )

        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0

        # Same resolver as /agent/run, /tools/mcp and the public catalog; settled before verify_credit.
        tool_cost = await resolve_tool_cost(tool_name, overrides, default_cost, True)

        # Optional caller consent bound; the debit below uses this same tool_cost, so charge <= cap.
        call_meta = params.get("_meta")
        max_cost = call_meta.get(MCP_MAX_COST_META_KEY) if isinstance(call_meta, dict) else None
        if max_cost is not None:
            if type(max_cost) is not int or max_cost < 0:
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        -32602,
                        f"_meta['{MCP_MAX_COST_META_KEY}'] must be a non-negative integer (atomic USDC).",
                    ),
                )
            if tool_cost > max_cost:
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        -32004,
                        f"Tool price {tool_cost} atomic USDC exceeds max_cost_usdc {max_cost}.",
                    ),
                )

        # ── Billing gate (credit-only for MCP calls) ──────────────────
        billing = BillingResult()
        if s.billing_enabled:
            billing = await verify_credit(org_id, tool_cost, principal_id=payload.get("sub") or None)
            if not billing.verified:
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        -32000,
                        billing.error or f"Insufficient credit balance. Required: {tool_cost} USDC atomic units.",
                    )
                )

        # ── Execute tool ──────────────────────────────────────────────
        if is_marketplace_tool:
            if tool_row is None:
                return JSONResponse(content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"))
            result = await _execute_marketplace_tool(tool_row, arguments)
        else:
            # Built-in tool execution (tool_def resolved + validated above)
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
        # Skip debit when execution failed: callers must not be charged
        # for infrastructure failures, breaker trips, or auth/decryption errors.
        execution_failed = isinstance(result, dict) and "error" in result
        debited = False
        if billing.verified and billing.billing_method == "credit" and not execution_failed:
            call_id = str(uuid.uuid4())
            debited, deducted = await debit_credit(
                org_id,
                tool_cost,
                reason=f"mcp:{tool_name}",
                principal_id=payload.get("sub") or None,
            )
            charge_id = await record_charge(
                source="mcp_v1",
                invocation_id=call_id,
                org_id=org_id or "",
                principal_id=payload.get("sub") or "",
                capability=tool_name,
                billing_method="credit",
                amount_usdc=tool_cost,
                status="settled" if debited else "failed",
                settled_amount_usdc=deducted,
            )
            if not debited and tool_cost > 0:
                # Tool already executed but the credit debit failed (e.g. a
                # concurrent debit drained the balance below the preflight
                # snapshot). Enqueue for asynchronous retry so the org is still
                # charged — mirrors agent_post_run.dispatch_settlement. The
                # gateway has no usage_event row, so the call id anchors the
                # usage_event_id, run_id and charge (pending_settlements has no FK).
                logger.warning("MCP debit failed org=%s tool=%s — enqueuing recovery", org_id, tool_name)
                try:
                    from billing.settlement import enqueue_failed_settlement

                    await enqueue_failed_settlement(
                        call_id,
                        org_id or "",
                        call_id,
                        "credit",
                        tool_cost,
                        principal_id=payload.get("sub") or None,
                        charge_id=charge_id,
                    )
                except Exception:
                    logger.exception("Failed to enqueue MCP settlement recovery org=%s", org_id)

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
