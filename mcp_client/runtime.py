# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Tool discovery, LangChain wrapping, and per-org aggregated tool building."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import StructuredTool

from mcp_client.base import (
    _MAX_RESPONSE_BYTES,
    _NAME_SEPARATOR,
    OrgMcpServer,
    logger,
)
from mcp_client.cache import _get_servers_cached
from mcp_client.session import _get_or_create_session
from teardrop.config import get_settings
from tools.shared import build_pydantic_model

McpServerLoader = OrgMcpServer | Callable[[], Awaitable[OrgMcpServer | None]]
McpSuccessHandler = Callable[[int], Awaitable[None]] | None
McpFailureHandler = Callable[[str, bool], Awaitable[None]] | None

# ─── Dynamic Pydantic model (shared helper) ───────────────────────────────────


def _build_pydantic_model(name: str, schema: dict[str, Any]) -> type:
    """Create a Pydantic model from an MCP tool's inputSchema."""
    return build_pydantic_model(name, schema)


# ─── Tool Discovery + LangChain Wrapping ─────────────────────────────────────


async def discover_mcp_tools(server: OrgMcpServer) -> list[dict[str, Any]]:
    """Connect to an MCP server and return raw tool definitions.

    Returns a list of dicts: [{"name": ..., "description": ..., "input_schema": ...}]
    Useful for the /discover endpoint.
    """
    settings = get_settings()
    session = await _get_or_create_session(server)

    response = await asyncio.wait_for(
        session.list_tools(),
        timeout=float(settings.mcp_client_connect_timeout_seconds),
    )

    tools: list[dict[str, Any]] = []
    for mcp_tool in response.tools[: settings.max_mcp_tools_per_server]:
        output_schema = getattr(mcp_tool, "outputSchema", None)
        if output_schema is None:
            output_schema = getattr(mcp_tool, "output_schema", None)
        tools.append(
            {
                "name": mcp_tool.name,
                "description": mcp_tool.description or "",
                "input_schema": mcp_tool.inputSchema or {},
                "output_schema": output_schema if isinstance(output_schema, dict) else None,
            }
        )
    return tools


def _extract_mcp_result_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    if structured is not None:
        return {"result": structured}

    if hasattr(result, "content") and result.content:
        parts: list[str] = []
        for part in result.content:
            text = getattr(part, "text", None)
            if text is None and isinstance(part, dict):
                text = part.get("text")
            if text is not None:
                parts.append(str(text))
        combined = "\n".join(parts)
        if len(combined) > _MAX_RESPONSE_BYTES:
            combined = combined[:_MAX_RESPONSE_BYTES] + "\n[TRUNCATED: MCP response exceeded 50 KB - content clipped]"
        try:
            parsed = json.loads(combined)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except (json.JSONDecodeError, ValueError):
            return {"result": combined}
    return {"result": str(result)}


async def _resolve_server(server_or_loader: McpServerLoader) -> OrgMcpServer | None:
    if isinstance(server_or_loader, OrgMcpServer):
        return server_or_loader
    return await server_or_loader()


def build_mcp_backed_tool(
    server_or_loader: McpServerLoader,
    mcp_tool_name: str,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    output_schema: dict[str, Any] | None = None,
    tool_id: str | None = None,
    on_success: McpSuccessHandler = None,
    on_failure: McpFailureHandler = None,
) -> StructuredTool:
    """Wrap an MCP tool as a LangChain StructuredTool with breaker support."""
    args_model = _build_pydantic_model(name, input_schema)
    _remote_tool_name = mcp_tool_name
    _tool_id = tool_id

    async def _call_mcp_tool(**kwargs: Any) -> dict[str, Any]:
        from tools.health import is_breaker_tripped, record_failure, record_success

        if _tool_id and await is_breaker_tripped(_tool_id):
            return {"error": "Tool temporarily unavailable (circuit breaker tripped)"}

        started = time.monotonic()

        async def _handle_failure(error_type: str, message: str) -> dict[str, Any]:
            tripped = False
            if _tool_id:
                tripped = await record_failure(_tool_id)
            if on_failure is not None:
                await on_failure(error_type, tripped)
            return {"error": message}

        try:
            server = await _resolve_server(server_or_loader)
        except Exception as exc:
            return await _handle_failure("server_lookup_failed", f"MCP server lookup failed: {type(exc).__name__}")

        if server is None:
            return await _handle_failure("server_unavailable", "MCP server unavailable")

        try:
            session = await _get_or_create_session(server)
            result = await asyncio.wait_for(
                session.call_tool(_remote_tool_name, kwargs),
                timeout=float(server.timeout_seconds),
            )
        except asyncio.TimeoutError:
            return await _handle_failure(
                "timeout",
                f"MCP tool '{_remote_tool_name}' timed out after {server.timeout_seconds}s",
            )
        except Exception as exc:
            return await _handle_failure(
                "upstream_error",
                f"MCP tool '{_remote_tool_name}' failed: {type(exc).__name__}",
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        if _tool_id:
            await record_success(_tool_id)
        if on_success is not None:
            await on_success(latency_ms)
        return _extract_mcp_result_payload(result)

    return StructuredTool.from_function(
        coroutine=_call_mcp_tool,
        name=name,
        description=description,
        args_schema=args_model,
        metadata={
            "output_schema": output_schema,
        },
    )


def _wrap_mcp_tool(
    server: OrgMcpServer,
    mcp_tool_name: str,
    mcp_tool_description: str,
    mcp_tool_input_schema: dict[str, Any],
    mcp_tool_output_schema: dict[str, Any] | None = None,
) -> StructuredTool:
    """Wrap a single MCP tool as a LangChain StructuredTool.

    The tool name is prefixed with the server name to prevent collisions:
    ``{server_name}__{tool_name}``
    """
    prefixed_name = f"{server.name}{_NAME_SEPARATOR}{mcp_tool_name}"
    return build_mcp_backed_tool(
        server,
        mcp_tool_name,
        prefixed_name,
        mcp_tool_description or f"Tool from MCP server '{server.name}'",
        mcp_tool_input_schema,
        output_schema=mcp_tool_output_schema,
    )


# ─── Per-org aggregated tool builder ──────────────────────────────────────────

_tools_cache: dict[str, tuple[list[StructuredTool], dict[str, StructuredTool], float]] = {}
# Key: org_id → (tools_list, tools_by_name, expires_at)
_tools_lock: asyncio.Lock | None = None


def _get_tools_lock() -> asyncio.Lock:
    global _tools_lock
    if _tools_lock is None:
        _tools_lock = asyncio.Lock()
    return _tools_lock


async def build_mcp_langchain_tools(
    org_id: str,
) -> tuple[list[StructuredTool], dict[str, StructuredTool]]:
    """Build LangChain ``StructuredTool`` wrappers for all active MCP servers for an org.

    Connects to each org MCP server over Streamable HTTP, calls ``list_tools()``,
    and wraps each remote tool as a LangChain ``StructuredTool`` with a prefixed
    name: ``{server.name}{_NAME_SEPARATOR}{mcp_tool.name}`` (double-underscore
    separator ``__``). This prevents collisions across multi-server orgs.

    Collision rules:
      * Any prefixed name that matches a global registry tool is silently skipped.
      * Duplicate prefixed names within the same aggregation pass are skipped.

    Results are cached per ``org_id`` with a TTL (``mcp_client_tool_cache_ttl_seconds``).
    Unreachable servers are skipped with a warning — tool discovery is best-effort.
    Returns ``(tools_list, tools_by_name_dict)``; both are empty when the org has no
    active servers.

    See also: ``_wrap_mcp_tool`` (wraps a single server tool) and
    ``discover_mcp_tools`` (raw tool listing for the /mcp/servers/{id}/discover endpoint).
    """
    settings = get_settings()

    # Check tool-level cache
    now = time.monotonic()
    cached = _tools_cache.get(org_id)
    if cached is not None:
        tools_list, tools_by_name, expiry = cached
        if now < expiry:
            return tools_list, tools_by_name

    async with _get_tools_lock():
        # Double-check after lock
        cached = _tools_cache.get(org_id)
        if cached is not None and time.monotonic() < cached[2]:
            return cached[0], cached[1]

        servers = await _get_servers_cached(org_id)
        if not servers:
            ttl = settings.mcp_client_tool_cache_ttl_seconds
            _tools_cache[org_id] = ([], {}, time.monotonic() + ttl)
            return [], {}

        from tools import registry as global_registry

        all_tools: list[StructuredTool] = []
        all_by_name: dict[str, StructuredTool] = {}

        for server in servers:
            try:
                session = await _get_or_create_session(server)
                response = await asyncio.wait_for(
                    session.list_tools(),
                    timeout=float(settings.mcp_client_connect_timeout_seconds),
                )
            except Exception:
                logger.warning(
                    "MCP server '%s' (org=%s) unreachable — skipping",
                    server.name,
                    org_id,
                    exc_info=True,
                )
                continue

            for mcp_tool in response.tools[: settings.max_mcp_tools_per_server]:
                prefixed_name = f"{server.name}{_NAME_SEPARATOR}{mcp_tool.name}"

                # Collision check: global registry
                if global_registry.get(mcp_tool.name) is not None:
                    logger.warning(
                        "MCP tool '%s' from server '%s' skipped — collides with global tool",
                        mcp_tool.name,
                        server.name,
                    )
                    continue

                # Collision check: already seen in this aggregation
                if prefixed_name in all_by_name:
                    continue

                try:
                    lc_tool = _wrap_mcp_tool(
                        server,
                        mcp_tool.name,
                        mcp_tool.description or "",
                        mcp_tool.inputSchema or {},
                        getattr(mcp_tool, "outputSchema", None),
                    )
                    all_tools.append(lc_tool)
                    all_by_name[prefixed_name] = lc_tool
                except Exception:
                    logger.warning(
                        "Failed to wrap MCP tool '%s' from server '%s'",
                        mcp_tool.name,
                        server.name,
                        exc_info=True,
                    )

        ttl = settings.mcp_client_tool_cache_ttl_seconds
        _tools_cache[org_id] = (all_tools, all_by_name, time.monotonic() + ttl)
        return all_tools, all_by_name
