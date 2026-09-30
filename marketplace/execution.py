# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""SSRF-pinned webhook execution for published marketplace tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any


async def execute_marketplace_tool(tool_row: dict[str, Any], arguments: dict[str, Any]) -> Any:
    """Execute a published marketplace tool via its webhook.

    ``tool_row`` is the raw DB row returned by ``get_marketplace_tool_by_name()``.
    Failures return ``{"error": ...}`` so callers can skip billing.
    """
    import time as _time  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415

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
