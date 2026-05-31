# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""MCP marketplace JSON-RPC gateway (POST /mcp/v1).

Implements the MCP Streamable HTTP JSON-RPC 2.0 endpoint that exposes both
built-in platform tools and published marketplace tools to external MCP clients.
Extracted verbatim from ``teardrop.routers.marketplace`` with no logic changes;
billing (credit-only gate), x402-free credit debit, SSRF validation, circuit
breaker, subscription gating, and author-earnings semantics are preserved exactly.

Methods handled by ``mcp_jsonrpc_handler``:
  * ``initialize`` – server capabilities / protocol version
  * ``tools/list`` – marketplace catalog + built-in tools with pricing
  * ``tools/call`` – subscription gate → credit billing gate → execute → debit →
    record author earnings + usage stats

Marketplace tool webhooks are invoked by ``_execute_marketplace_tool``, which
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
    verify_credit,
)
from marketplace import (
    check_org_subscription,
    get_marketplace_catalog,
    get_marketplace_tool_by_name,
    record_marketplace_tool_usage_many,
    record_tool_call_earnings,
)
from teardrop._meta import APP_VERSION
from teardrop.config import get_settings
from teardrop.dependencies import require_auth
from teardrop.rate_limit import _enforce_rate_limit
from tools import registry
from tools.executor import execute_tool

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── MCP Marketplace – JSON-RPC Handler ─────────────────────────────────────


async def _execute_marketplace_tool(tool_row: dict[str, Any], arguments: dict[str, Any]) -> Any:
    """Execute a published marketplace tool via its webhook.

    ``tool_row`` is the raw DB row returned by ``get_marketplace_tool_by_name()``.
    Follows the same SSRF-safe webhook pattern as ``_build_langchain_tool``.
    """
    import time as _time  # noqa: PLC0415

    import aiohttp  # noqa: PLC0415

    from org_tools import _decrypt_header, _hash_webhook_host, _on_webhook_failure, _record_event  # noqa: PLC0415
    from tools.definitions.http_fetch import (  # noqa: PLC0415
        async_validate_url_with_ips,
        make_ssrf_safe_connector,
    )
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

    url_err, validated_ips = await async_validate_url_with_ips(url)
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
    from urllib.parse import urlparse  # noqa: PLC0415

    hostname = urlparse(url).hostname or ""
    connector = make_ssrf_safe_connector(hostname, validated_ips)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
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

        # ── Resolve marketplace tool + final price BEFORE the billing gate ──
        # The author's base_price_usdc may exceed the default cost, so the row
        # must be fetched and the price settled before verify_credit; otherwise
        # the preflight would approve the wrong (lower) amount and the tool
        # would execute against a balance that cannot cover the real cost.
        result: Any
        author_org_id: str | None = None
        tool_row: dict | None = None

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

        # ── Validate built-in tool arguments BEFORE the billing gate ──
        # Built-in tools are invoked through their raw implementation coroutine
        # (`tool_def.implementation`), which bypasses the LangChain/Pydantic
        # argument coercion that the agent runtime relies on. Without an explicit
        # check, malformed `arguments` would reach the tool body and surface as an
        # unclassified runtime error — after the caller had already been billed.
        # Validate here so rejected calls are never charged.
        if not is_marketplace_tool:
            tool_def = registry.get(tool_name)
            if tool_def is None:
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"),
                )
            if not isinstance(arguments, dict):
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32602, f"Invalid arguments for tool '{tool_name}': expected an object"),
                )
            try:
                tool_def.input_schema(**arguments)
            except PydanticValidationError:
                logger.info("mcp/v1 invalid arguments org_id=%s tool=%s", org_id, tool_name)
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32602, f"Invalid arguments for tool '{tool_name}'"),
                )

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

        # ── Execute tool ──────────────────────────────────────────────
        if is_marketplace_tool:
            assert tool_row is not None  # resolved above for marketplace tools
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
        # Skip debit when execution failed: subscribers must not be charged
        # for infrastructure failures, breaker trips, or auth/decryption errors.
        execution_failed = isinstance(result, dict) and "error" in result
        debited = False
        if billing.verified and billing.billing_method == "credit" and not execution_failed:
            debited, _ = await debit_credit(org_id, tool_cost, reason=f"mcp:{tool_name}")
            if not debited and tool_cost > 0:
                # Tool already executed but the credit debit failed (e.g. a
                # concurrent debit drained the balance below the preflight
                # snapshot). Enqueue for asynchronous retry so the org is still
                # charged — mirrors agent_post_run.dispatch_settlement. The
                # gateway has no usage_event row, so a synthetic UUID anchors
                # both ids (pending_settlements has no FK).
                logger.warning("MCP debit failed org=%s tool=%s — enqueuing recovery", org_id, tool_name)
                try:
                    from billing.settlement import enqueue_failed_settlement

                    call_id = str(uuid.uuid4())
                    await enqueue_failed_settlement(call_id, org_id or "", call_id, "credit", tool_cost)
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
