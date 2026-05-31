# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Webhook execution runtime and LangChain tool building for the org-tool registry.

Converts stored :class:`OrgTool` records into LangChain ``StructuredTool``
instances that call the org's webhook with SSRF validation, IP pinning, timeout,
response truncation, circuit-breaker enforcement, and an immutable audit trail.
Also exposes JSON-Schema → Pydantic model helpers and webhook response
normalisation.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiohttp
import sentry_sdk
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from org_tools.base import (
    _MAX_RESPONSE_BYTES,
    OrgTool,
    _decrypt_header,
    _get_pool,
    _record_event,
)
from org_tools.cache import get_org_tools_cached
from shared.webhook import WebhookCaller, WebhookCallError
from tools.definitions.http_fetch import async_validate_url_with_ips, make_ssrf_safe_connector
from tools.shared import build_pydantic_model
from tools.shared import (
    validate_safe_schema_subset as _validate_safe_schema_subset,
)

logger = logging.getLogger(__name__)


# ─── Dynamic Pydantic model from JSON Schema ─────────────────────────────────


def normalize_webhook_response(
    raw: bytes,
    *,
    content_type: str,
    status_code: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Normalize webhook HTTP response payload into a consistent shape."""
    body_bytes = raw[:max_bytes]

    if "application/json" not in content_type:
        return {
            "success": False,
            "status_code": status_code,
            "response_body": None,
            "error": f"Webhook returned non-JSON Content-Type: {content_type or 'unset'}",
            "error_type": "non_json_response",
        }

    try:
        parsed = json.loads(body_bytes)
    except json.JSONDecodeError:
        return {
            "success": False,
            "status_code": status_code,
            "response_body": None,
            "error": "Webhook returned invalid or truncated JSON",
            "error_type": "invalid_json",
        }

    response_body = parsed if isinstance(parsed, dict) else {"value": parsed}

    if status_code >= 400:
        return {
            "success": False,
            "status_code": status_code,
            "response_body": response_body,
            "error": f"Webhook returned HTTP {status_code}",
            "error_type": "http_error",
        }

    return {
        "success": True,
        "status_code": status_code,
        "response_body": response_body,
        "error": None,
        "error_type": None,
    }


def validate_safe_schema_subset(schema: dict[str, Any]) -> list[str]:
    """Return unsupported schema keywords/types that runtime tooling cannot enforce."""
    return _validate_safe_schema_subset(schema)


def _build_pydantic_model(
    name: str,
    schema: dict[str, Any],
    model_name: str | None = None,
) -> type[BaseModel]:
    """Backward-compatible wrapper around the shared Pydantic model builder."""
    return build_pydantic_model(name, schema, model_name=model_name)


# ─── Webhook execution & LangChain tool building ─────────────────────────────


def _hash_webhook_host(url: str) -> str:
    """Return a short, non-reversible host fingerprint for Sentry tagging.

    Avoids leaking full URLs (which may include path tokens) while still
    permitting cluster-level analysis of failing hosts.
    """
    import hashlib
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc or "unknown"
    except Exception:
        host = "unknown"
    return hashlib.sha256(host.encode()).hexdigest()[:12]


def _build_langchain_tool(
    tool: OrgTool,
    auth_header_name: str | None,
    auth_header_enc: str | None,
) -> StructuredTool:
    """Convert a stored OrgTool into a LangChain StructuredTool.

    The returned tool calls the webhook URL with SSRF validation, timeout,
    and response truncation.
    """
    args_model = _build_pydantic_model(tool.name, tool.input_schema)

    # Capture values in closure — avoid mutable state.
    _tool_id = tool.id
    _org_id = tool.org_id
    _tool_name = tool.name
    _url = tool.webhook_url
    _method = tool.webhook_method
    _timeout = tool.timeout_seconds
    _header_name = auth_header_name
    _header_enc = auth_header_enc
    _host_hash = _hash_webhook_host(_url)
    caller = WebhookCaller(
        url=_url,
        timeout_seconds=_timeout,
        auth_header_name=_header_name,
        auth_header_encrypted=_header_enc,
        max_response_bytes=_MAX_RESPONSE_BYTES,
    )

    if _method != "GET":
        raise ValueError(f"Org tool '{tool.name}' has non-GET webhook_method '{_method}'")

    async def _handle_call_error(exc: WebhookCallError) -> dict[str, Any]:
        if exc.error_type == "ssrf_blocked":
            return {"error": exc.message}
        if exc.error_type == "decrypt_failure":
            await _on_webhook_failure(_tool_id, _org_id, _tool_name, _host_hash, "decrypt_failure")
            return {"error": exc.message}
        await _on_webhook_failure(_tool_id, _org_id, _tool_name, _host_hash, exc.error_type)
        return {"error": exc.message}

    async def _call_webhook(**kwargs: Any) -> dict[str, Any]:
        from tools.health import is_breaker_tripped, record_success

        # Pre-execution gate: skip immediately if breaker is tripped.
        if await is_breaker_tripped(_tool_id):
            return {"error": "Tool temporarily unavailable (circuit breaker tripped)"}

        started = time.monotonic()
        try:
            call_result = await caller.call_get(
                params=kwargs,
                decrypt_header=_decrypt_header,
                validate_url=async_validate_url_with_ips,
                client_session_factory=aiohttp.ClientSession,
                connector_factory=make_ssrf_safe_connector,
            )
        except WebhookCallError as exc:
            return await _handle_call_error(exc)

        normalized = normalize_webhook_response(
            call_result.body,
            content_type=call_result.content_type,
            status_code=call_result.status_code,
            max_bytes=_MAX_RESPONSE_BYTES,
        )

        if not normalized["success"]:
            await _on_webhook_failure(
                _tool_id,
                _org_id,
                _tool_name,
                _host_hash,
                normalized["error_type"] or "upstream_error",
                status_code=call_result.status_code,
            )
            return {"error": normalized["error"], "status": call_result.status_code}

        # Success path.
        latency_ms = int((time.monotonic() - started) * 1000)
        await record_success(_tool_id)
        await _record_event(
            _org_id,
            _tool_id,
            _tool_name,
            "executed",
            actor_id="agent",
            detail={"latency_ms": latency_ms, "status": call_result.status_code},
        )
        return normalized["response_body"]

    return StructuredTool.from_function(
        coroutine=_call_webhook,
        name=tool.name,
        description=tool.description,
        args_schema=args_model,
        metadata={
            "timeout_seconds": tool.timeout_seconds,
            "output_schema": tool.output_schema,
        },
    )


async def _on_webhook_failure(
    tool_id: str,
    org_id: str,
    tool_name: str,
    host_hash: str,
    error_type: str,
    *,
    status_code: int | None = None,
) -> None:
    """Centralised failure side-effects: audit, breaker, sentry, deactivation."""
    from tools.health import record_failure

    detail: dict[str, Any] = {"error_type": error_type, "host_hash": host_hash}
    if status_code is not None:
        detail["status"] = status_code
    await _record_event(
        org_id,
        tool_id,
        tool_name,
        "failed",
        actor_id="agent",
        detail=detail,
    )

    tripped = False
    try:
        tripped = await record_failure(tool_id)
    except Exception:  # pragma: no cover
        logger.warning("tool_health.record_failure raised", exc_info=True)

    # Sentry: capture only on tripped transitions to avoid quota burn from
    # flapping tools.  Per-failure breadcrumbs are still emitted via logger.
    if tripped:
        try:
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("tool_id", str(tool_id))
                scope.set_tag("org_id", str(org_id))
                scope.set_tag("error_type", error_type)
                scope.set_tag("webhook_host", host_hash)
                scope.set_tag("circuit_breaker", "tripped")
                sentry_sdk.capture_message(
                    f"Webhook circuit breaker tripped: tool_id={tool_id}",
                    level="warning",
                )
        except Exception:  # pragma: no cover
            logger.debug("sentry capture failed in _on_webhook_failure", exc_info=True)

        try:
            from marketplace import auto_deactivate_tool_for_health

            await auto_deactivate_tool_for_health(tool_id)
        except Exception:  # pragma: no cover
            logger.warning("auto_deactivate_tool_for_health failed tool_id=%s", tool_id, exc_info=True)


async def build_org_langchain_tools(
    org_id: str,
) -> tuple[list[StructuredTool], dict[str, StructuredTool]]:
    """Build LangChain tools for all active tools belonging to an org.

    Returns ``(tools_list, tools_by_name_dict)``.
    Tools whose names collide with a global registry tool are skipped.
    """
    from tools import registry as global_registry

    org_tools = await get_org_tools_cached(org_id)
    if not org_tools:
        return [], {}

    # We need auth data to build the tools — fetch raw rows.
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT id, auth_header_name, auth_header_enc FROM org_tools WHERE org_id = $1 AND is_active = TRUE",
        org_id,
    )
    auth_lookup = {r["id"]: (r["auth_header_name"], r["auth_header_enc"]) for r in rows}

    tools_list: list[StructuredTool] = []
    tools_by_name: dict[str, StructuredTool] = {}

    for ot in org_tools:
        # Skip if collides with a global tool
        if global_registry.get(ot.name) is not None:
            logger.warning(
                "Org tool '%s' (org=%s) skipped — collides with global tool",
                ot.name,
                org_id,
            )
            continue

        if ot.webhook_method != "GET":
            logger.warning(
                "Org tool '%s' (org=%s) skipped — non-GET webhook_method '%s' not permitted",
                ot.name,
                org_id,
                ot.webhook_method,
            )
            continue

        auth_name, auth_enc = auth_lookup.get(ot.id, (None, None))
        try:
            lc_tool = _build_langchain_tool(ot, auth_name, auth_enc)
            tools_list.append(lc_tool)
            tools_by_name[ot.name] = lc_tool
        except Exception:
            logger.warning("Failed to build org tool '%s'", ot.name, exc_info=True)

    return tools_list, tools_by_name
