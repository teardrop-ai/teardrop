# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Tool discovery, LangChain wrapping, and per-org aggregated tool building."""

from __future__ import annotations

import asyncio
import hashlib
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
from mcp_client.cache import (
    _get_servers_cached,
    get_mcp_tools_cache_version,
    invalidate_mcp_cache,
    refresh_mcp_servers,
)
from mcp_client.crud import record_mcp_server_schema_hash
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


def _mcp_tools_schema_hash(tools: list[dict[str, Any]]) -> str:
    """Return a stable hash of safe, agent-visible MCP tool metadata."""
    canonical_tools = [
        {
            "name": str(tool.get("name", "")),
            "description": str(tool.get("description", "")),
            "input_schema": tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else {},
            "output_schema": tool.get("output_schema") if isinstance(tool.get("output_schema"), dict) else None,
        }
        for tool in tools
    ]
    canonical_tools.sort(key=lambda tool: (tool["name"], json.dumps(tool, sort_keys=True, separators=(",", ":"))))
    payload = json.dumps(canonical_tools, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _track_mcp_schema_discovery(server: OrgMcpServer, tools: list[dict[str, Any]]) -> bool:
    """Persist a successful discovery hash without making discovery depend on telemetry."""
    try:
        updated, schema_changed = await record_mcp_server_schema_hash(server, _mcp_tools_schema_hash(tools))
    except Exception:
        logger.warning("MCP schema tracking failed server_id=%s", server.id, exc_info=True)
        return False

    if updated:
        try:
            await invalidate_mcp_cache(server.org_id)
        except Exception:
            logger.warning("MCP schema cache invalidation failed server_id=%s", server.id, exc_info=True)
    return schema_changed


async def discover_mcp_tools_with_schema(server: OrgMcpServer) -> tuple[list[dict[str, Any]], bool]:
    """Connect to an MCP server, return tool definitions, and report confirmed drift.

    Returns a list of dicts: [{"name": ..., "description": ..., "input_schema": ...}]
    plus whether a previously known inventory changed. The initial successful
    discovery establishes a baseline and returns ``False`` for drift.
    """
    settings = get_settings()
    started = time.monotonic()
    try:
        session = await _get_or_create_session(server)
        response = await asyncio.wait_for(
            session.list_tools(),
            timeout=float(settings.mcp_client_connect_timeout_seconds),
        )
    except Exception:
        logger.warning(
            "MCP tool discovery failed server_id=%s elapsed_ms=%d",
            server.id,
            int((time.monotonic() - started) * 1000),
            exc_info=True,
        )
        raise

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
    logger.info(
        "MCP tool discovery succeeded server_id=%s tool_count=%d elapsed_ms=%d",
        server.id,
        len(tools),
        int((time.monotonic() - started) * 1000),
    )
    return tools, await _track_mcp_schema_discovery(server, tools)


async def discover_mcp_tools(server: OrgMcpServer) -> list[dict[str, Any]]:
    """Connect to an MCP server and return raw tool definitions.

    Kept for backward compatibility; use ``discover_mcp_tools_with_schema``
    when callers need the drift signal.
    """
    tools, _ = await discover_mcp_tools_with_schema(server)
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

_tools_cache: dict[str, tuple[list[StructuredTool], dict[str, StructuredTool], float, int | None]] = {}
# Key: org_id → (tools_list, tools_by_name, expires_at, shared_cache_version)
_tools_lock: asyncio.Lock | None = None


def _get_tools_lock() -> asyncio.Lock:
    global _tools_lock
    if _tools_lock is None:
        _tools_lock = asyncio.Lock()
    return _tools_lock


async def _get_current_tools_cache(
    org_id: str,
) -> tuple[list[StructuredTool], dict[str, StructuredTool]] | None:
    cached = _tools_cache.get(org_id)
    if cached is None or time.monotonic() >= cached[2]:
        return None

    shared_version = await get_mcp_tools_cache_version(org_id)
    if shared_version is not None and shared_version != cached[3]:
        _tools_cache.pop(org_id, None)
        return None
    return cached[0], cached[1]


async def _build_mcp_tools_for_servers(
    org_id: str,
    servers: list[OrgMcpServer],
) -> tuple[list[StructuredTool], dict[str, StructuredTool]]:
    """Discover and wrap an already-resolved list of active MCP servers."""
    from tools import registry as global_registry

    all_tools: list[StructuredTool] = []
    all_by_name: dict[str, StructuredTool] = {}

    for server in servers:
        try:
            discovered_tools, _ = await discover_mcp_tools_with_schema(server)
        except Exception:
            logger.warning(
                "MCP server '%s' (org=%s) unreachable — skipping",
                server.name,
                org_id,
                exc_info=True,
            )
            continue

        for mcp_tool in discovered_tools:
            mcp_tool_name = str(mcp_tool["name"])
            prefixed_name = f"{server.name}{_NAME_SEPARATOR}{mcp_tool_name}"

            # Collision check: global registry
            if global_registry.get(mcp_tool_name) is not None:
                logger.warning(
                    "MCP tool '%s' from server '%s' skipped — collides with global tool",
                    mcp_tool_name,
                    server.name,
                )
                continue

            # Collision check: already seen in this aggregation
            if prefixed_name in all_by_name:
                continue

            try:
                lc_tool = _wrap_mcp_tool(
                    server,
                    mcp_tool_name,
                    str(mcp_tool["description"]),
                    mcp_tool["input_schema"],
                    mcp_tool["output_schema"],
                )
                all_tools.append(lc_tool)
                all_by_name[prefixed_name] = lc_tool
            except Exception:
                logger.warning(
                    "Failed to wrap MCP tool '%s' from server '%s'",
                    mcp_tool_name,
                    server.name,
                    exc_info=True,
                )

    return all_tools, all_by_name


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
    cached_tools = await _get_current_tools_cache(org_id)
    if cached_tools is not None:
        return cached_tools

    async with _get_tools_lock():
        # Double-check after lock
        cached_tools = await _get_current_tools_cache(org_id)
        if cached_tools is not None:
            return cached_tools

        for attempt in range(2):
            cache_version_before = await get_mcp_tools_cache_version(org_id)
            servers = await refresh_mcp_servers(org_id) if cache_version_before is not None else await _get_servers_cached(org_id)
            all_tools, all_by_name = await _build_mcp_tools_for_servers(org_id, servers)
            cache_version_after = await get_mcp_tools_cache_version(org_id)

            if cache_version_before == cache_version_after:
                ttl = settings.mcp_client_tool_cache_ttl_seconds
                _tools_cache[org_id] = (
                    all_tools,
                    all_by_name,
                    time.monotonic() + ttl,
                    cache_version_after,
                )
                return all_tools, all_by_name

            logger.debug(
                "MCP tools cache version changed during build org=%s attempt=%d; rebuilding",
                org_id,
                attempt + 1,
            )

        # A continuously changing remote inventory is returned uncached rather
        # than marking a stale snapshot as current.
        return all_tools, all_by_name
