# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Tool discovery, LangChain wrapping, and per-org aggregated tool building."""

from __future__ import annotations

import asyncio
import json
import time
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
        tools.append(
            {
                "name": mcp_tool.name,
                "description": mcp_tool.description or "",
                "input_schema": mcp_tool.inputSchema or {},
            }
        )
    return tools


def _wrap_mcp_tool(
    server: OrgMcpServer,
    session: Any,
    mcp_tool_name: str,
    mcp_tool_description: str,
    mcp_tool_input_schema: dict[str, Any],
) -> StructuredTool:
    """Wrap a single MCP tool as a LangChain StructuredTool.

    The tool name is prefixed with the server name to prevent collisions:
    ``{server_name}__{tool_name}``
    """
    prefixed_name = f"{server.name}{_NAME_SEPARATOR}{mcp_tool_name}"
    args_model = _build_pydantic_model(prefixed_name, mcp_tool_input_schema)

    # Capture in closure
    _session = session
    _tool_name = mcp_tool_name
    _timeout = server.timeout_seconds

    async def _call_mcp_tool(**kwargs: Any) -> dict[str, Any]:
        try:
            result = await asyncio.wait_for(
                _session.call_tool(_tool_name, kwargs),
                timeout=float(_timeout),
            )
            # Extract text content from MCP result
            if hasattr(result, "content") and result.content:
                parts = []
                for part in result.content:
                    if hasattr(part, "text"):
                        parts.append(part.text)
                combined = "\n".join(parts)
                if len(combined) > _MAX_RESPONSE_BYTES:
                    combined = combined[:_MAX_RESPONSE_BYTES] + "\n[TRUNCATED: MCP response exceeded 50 KB - content clipped]"
                # Try to parse as JSON for structured output
                try:
                    return json.loads(combined)
                except (json.JSONDecodeError, ValueError):
                    return {"result": combined}
            return {"result": str(result)}
        except asyncio.TimeoutError:
            return {"error": f"MCP tool '{_tool_name}' timed out after {_timeout}s"}
        except Exception as exc:
            return {"error": f"MCP tool '{_tool_name}' failed: {type(exc).__name__}"}

    return StructuredTool.from_function(
        coroutine=_call_mcp_tool,
        name=prefixed_name,
        description=mcp_tool_description or f"Tool from MCP server '{server.name}'",
        args_schema=args_model,
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
                        session,
                        mcp_tool.name,
                        mcp_tool.description or "",
                        mcp_tool.inputSchema or {},
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
