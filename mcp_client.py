# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-org MCP client connections — CRUD, session pool, tool discovery, and caching.

Allows organisations to register external MCP servers whose tools are
dynamically discovered, wrapped as LangChain StructuredTool objects, and
injected into the agent at run time alongside global and org webhook tools.

Transport: Streamable HTTP only (no stdio). Teardrop is a multi-tenant
server — spawning subprocesses per request is not viable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Literal

import asyncpg
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from cache import TTLCache, get_redis
from config import get_settings

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_MAX_RESPONSE_BYTES = 50 * 1024  # 50 KB response cap per MCP tool call
_NAME_SEPARATOR = "__"  # server_name__tool_name

# ─── Models ───────────────────────────────────────────────────────────────────


class OrgMcpServer(BaseModel):
    """Public representation of an MCP server config (never contains raw auth tokens)."""

    id: str
    org_id: str
    name: str
    url: str
    auth_type: Literal["none", "bearer", "header"]
    has_auth: bool
    auth_header_name: str | None = None
    is_active: bool
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime


# ─── Database pool ────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_mcp_client_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference.  Called during app lifespan startup."""
    global _pool
    _pool = pool
    logger.info("MCP client DB ready")


async def close_mcp_client_db() -> None:
    """Release the pool reference and close all cached MCP sessions."""
    global _pool
    await _close_all_sessions()
    if _pool is not None:
        _pool = None
        logger.info("MCP client DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("MCP client DB not initialised — call init_mcp_client_db() first")
    return _pool


# ─── Encryption (reuse org_tools Fernet) ──────────────────────────────────────


def _encrypt_token(value: str) -> str:
    """Encrypt an auth token for at-rest storage."""
    from org_tools import _encrypt_header

    return _encrypt_header(value)


def _decrypt_token(encrypted: str) -> str:
    """Decrypt a stored auth token."""
    from org_tools import _decrypt_header

    return _decrypt_header(encrypted)


# ─── Audit logging ────────────────────────────────────────────────────────────


async def _record_event(
    org_id: str,
    server_id: str,
    server_name: str,
    event_type: str,
    actor_id: str = "",
    detail: str = "",
) -> None:
    """Insert an immutable audit event.  Best-effort — errors logged, never raised."""
    try:
        pool = _get_pool()
        await pool.execute(
            "INSERT INTO org_mcp_server_events"
            " (id, org_id, server_id, server_name, event_type, detail, actor_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)",
            str(uuid.uuid4()),
            org_id,
            server_id,
            server_name,
            event_type,
            detail,
            actor_id,
        )
    except Exception:
        logger.warning("Failed to record MCP server event", exc_info=True)


# ─── CRUD ─────────────────────────────────────────────────────────────────────


async def create_org_mcp_server(
    org_id: str,
    *,
    name: str,
    url: str,
    auth_type: str = "none",
    auth_token: str | None = None,
    auth_header_name: str | None = None,
    timeout_seconds: int = 15,
    actor_id: str,
) -> OrgMcpServer:
    """Insert a new MCP server.  Raises ValueError on quota/duplicate/validation errors."""
    pool = _get_pool()
    settings = get_settings()

    # URL validation (SSRF prevention)
    from tools.definitions.http_fetch import async_validate_url

    url_error = await async_validate_url(url)
    if url_error is not None:
        raise ValueError(f"URL blocked: {url_error}")

    # HTTPS enforcement in production
    if settings.app_env == "production" and not url.startswith("https://"):
        raise ValueError("MCP server URL must use HTTPS in production")

    # Quota check
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM org_mcp_servers WHERE org_id = $1 AND is_active = TRUE",
        org_id,
    )
    if count >= settings.max_org_mcp_servers:
        raise ValueError(f"MCP server limit reached ({settings.max_org_mcp_servers})")

    server_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    auth_enc: str | None = None
    if auth_token:
        auth_enc = _encrypt_token(auth_token)

    try:
        await pool.execute(
            "INSERT INTO org_mcp_servers"
            " (id, org_id, name, url, auth_type, auth_token_enc, auth_header_name,"
            "  timeout_seconds, is_active, created_at, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE, $9, $9)",
            server_id,
            org_id,
            name,
            url,
            auth_type,
            auth_enc,
            auth_header_name,
            timeout_seconds,
            now,
        )
    except asyncpg.UniqueViolationError:
        raise ValueError(f"MCP server '{name}' already exists for this organisation")

    await _record_event(org_id, server_id, name, "created", actor_id)
    await invalidate_mcp_cache(org_id)

    return OrgMcpServer(
        id=server_id,
        org_id=org_id,
        name=name,
        url=url,
        auth_type=auth_type,
        has_auth=auth_token is not None,
        auth_header_name=auth_header_name,
        is_active=True,
        timeout_seconds=timeout_seconds,
        created_at=now,
        updated_at=now,
    )


def _row_to_model(row: asyncpg.Record) -> OrgMcpServer:
    """Map a DB row to an OrgMcpServer model."""
    return OrgMcpServer(
        id=row["id"],
        org_id=row["org_id"],
        name=row["name"],
        url=row["url"],
        auth_type=row["auth_type"],
        has_auth=row["auth_token_enc"] is not None,
        auth_header_name=row["auth_header_name"],
        is_active=row["is_active"],
        timeout_seconds=row["timeout_seconds"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_org_mcp_server(server_id: str, org_id: str) -> OrgMcpServer | None:
    """Return a single server scoped to the org, or None."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM org_mcp_servers WHERE id = $1 AND org_id = $2",
        server_id,
        org_id,
    )
    return _row_to_model(row) if row else None


async def list_org_mcp_servers(org_id: str, *, active_only: bool = True) -> list[OrgMcpServer]:
    """List all MCP servers for an org."""
    pool = _get_pool()
    query = "SELECT * FROM org_mcp_servers WHERE org_id = $1"
    if active_only:
        query += " AND is_active = TRUE"
    query += " ORDER BY name"
    rows = await pool.fetch(query, org_id)
    return [_row_to_model(r) for r in rows]


async def update_org_mcp_server(
    server_id: str,
    org_id: str,
    *,
    actor_id: str,
    name: str | None = None,
    url: str | None = None,
    auth_type: str | None = None,
    auth_token: str | None = ...,  # type: ignore[assignment]
    auth_header_name: str | None = ...,  # type: ignore[assignment]
    timeout_seconds: int | None = None,
    is_active: bool | None = None,
) -> OrgMcpServer | None:
    """Partial-update a server.  Returns updated model or None if not found."""
    pool = _get_pool()

    row = await pool.fetchrow(
        "SELECT * FROM org_mcp_servers WHERE id = $1 AND org_id = $2",
        server_id,
        org_id,
    )
    if row is None:
        return None

    sets: list[str] = []
    params: list[Any] = []
    idx = 1

    def _add(col: str, val: Any) -> None:
        nonlocal idx
        sets.append(f"{col} = ${idx}")
        params.append(val)
        idx += 1

    if name is not None:
        _add("name", name)
    if url is not None:
        from tools.definitions.http_fetch import async_validate_url

        url_error = await async_validate_url(url)
        if url_error is not None:
            raise ValueError(f"URL blocked: {url_error}")
        settings = get_settings()
        if settings.app_env == "production" and not url.startswith("https://"):
            raise ValueError("MCP server URL must use HTTPS in production")
        _add("url", url)
    if auth_type is not None:
        _add("auth_type", auth_type)
    if timeout_seconds is not None:
        _add("timeout_seconds", timeout_seconds)
    if is_active is not None:
        _add("is_active", is_active)

    # Handle auth token updates (sentinel ... means "not provided")
    if auth_header_name is not ...:
        _add("auth_header_name", auth_header_name)
    if auth_token is not ...:
        if auth_token is not None:
            _add("auth_token_enc", _encrypt_token(auth_token))
        else:
            _add("auth_token_enc", None)

    if not sets:
        return _row_to_model(row)

    _add("updated_at", datetime.now(timezone.utc))
    params.append(server_id)
    params.append(org_id)

    query = f"UPDATE org_mcp_servers SET {', '.join(sets)} WHERE id = ${idx} AND org_id = ${idx + 1} RETURNING *"
    updated = await pool.fetchrow(query, *params)

    await _record_event(org_id, server_id, row["name"], "updated", actor_id)
    await _evict_session(server_id)
    await invalidate_mcp_cache(org_id)

    return _row_to_model(updated) if updated else None


async def delete_org_mcp_server(server_id: str, org_id: str, *, actor_id: str) -> bool:
    """Soft-delete a server (set is_active=False).  Returns True if found."""
    pool = _get_pool()
    result = await pool.execute(
        "UPDATE org_mcp_servers SET is_active = FALSE, updated_at = NOW() WHERE id = $1 AND org_id = $2 AND is_active = TRUE",
        server_id,
        org_id,
    )
    deleted = result.split()[-1] != "0"  # "UPDATE N"
    if deleted:
        row = await pool.fetchrow("SELECT name FROM org_mcp_servers WHERE id = $1", server_id)
        name = row["name"] if row else server_id
        await _record_event(org_id, server_id, name, "deleted", actor_id)
        await _evict_session(server_id)
        await invalidate_mcp_cache(org_id)
    return deleted


# ─── Per-org TTL cache (server list) ──────────────────────────────────────────

_server_caches: dict[str, TTLCache[list[OrgMcpServer]]] = {}


def _get_server_cache(org_id: str) -> TTLCache[list[OrgMcpServer]]:
    if org_id not in _server_caches:
        _server_caches[org_id] = TTLCache(
            name=f"org_mcp_servers:{org_id}",
            redis_key=f"teardrop:org_mcp_servers:{org_id}",
            ttl_seconds_fn=lambda: get_settings().mcp_client_tool_cache_ttl_seconds,
            loader=lambda: list_org_mcp_servers(org_id, active_only=True),
            serialize=lambda servers: json.dumps([s.model_dump(mode="json") for s in servers]),
            deserialize=lambda raw: [OrgMcpServer(**item) for item in json.loads(raw)],
        )
    return _server_caches[org_id]


async def _get_servers_cached(org_id: str) -> list[OrgMcpServer]:
    """Return active MCP servers for an org with TTL cache (Redis → in-process)."""
    return await _get_server_cache(org_id).get() or []


async def invalidate_mcp_cache(org_id: str) -> None:
    """Clear the server list cache and tool cache for an org."""
    await _get_server_cache(org_id).invalidate()
    _tools_cache.pop(org_id, None)
    r = get_redis()
    if r is not None:
        try:
            await r.delete(f"teardrop:org_mcp_tools:{org_id}")
        except Exception:
            logger.warning("Redis MCP tools cache invalidation failed (non-fatal)", exc_info=True)
    _tools_cache.pop(org_id, None)
    r = get_redis()
    if r is not None:
        try:
            await r.delete(f"teardrop:org_mcp_tools:{org_id}")
        except Exception:
            logger.warning("Redis MCP tools cache invalidation failed (non-fatal)", exc_info=True)


# ─── MCP Session Pool ────────────────────────────────────────────────────────

_sessions: dict[str, tuple[Any, AsyncExitStack, float]] = {}
# Key: server_id → (ClientSession, exit_stack, expires_at_monotonic)


async def _get_or_create_session(server: OrgMcpServer) -> Any:
    """Return a cached MCP ClientSession or create a new one.

    Uses Streamable HTTP transport with optional auth headers.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    settings = get_settings()
    now = time.monotonic()

    # Check cache hit
    cached = _sessions.get(server.id)
    if cached is not None:
        session, _, expires_at = cached
        if now < expires_at:
            return session
        # Expired — close and recreate
        await _evict_session(server.id)

    # Build auth headers
    headers: dict[str, str] = {}
    if server.has_auth:
        pool = _get_pool()
        row = await pool.fetchrow(
            "SELECT auth_type, auth_token_enc, auth_header_name FROM org_mcp_servers WHERE id = $1",
            server.id,
        )
        if row and row["auth_token_enc"]:
            token = _decrypt_token(row["auth_token_enc"])
            if row["auth_type"] == "bearer":
                headers["Authorization"] = f"Bearer {token}"
            elif row["auth_type"] == "header" and row["auth_header_name"]:
                headers[row["auth_header_name"]] = token

    # Connect via Streamable HTTP
    exit_stack = AsyncExitStack()
    try:
        transport = await exit_stack.enter_async_context(
            streamablehttp_client(
                url=server.url,
                headers=headers or None,
                timeout=float(settings.mcp_client_connect_timeout_seconds),
            )
        )
        read_stream, write_stream, _ = transport
        session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
        await asyncio.wait_for(
            session.initialize(),
            timeout=float(settings.mcp_client_connect_timeout_seconds),
        )
    except Exception as exc:
        await exit_stack.aclose()
        await _record_event(
            server.org_id,
            server.id,
            server.name,
            "connection_failed",
            detail=str(exc)[:200],
        )
        raise

    ttl = settings.mcp_client_tool_cache_ttl_seconds
    _sessions[server.id] = (session, exit_stack, time.monotonic() + ttl)

    await _record_event(server.org_id, server.id, server.name, "connected")
    return session


async def _evict_session(server_id: str) -> None:
    """Close and remove a cached session."""
    entry = _sessions.pop(server_id, None)
    if entry is not None:
        _, exit_stack, _ = entry
        try:
            await exit_stack.aclose()
        except Exception:
            logger.debug("Error closing MCP session %s", server_id, exc_info=True)


async def _close_all_sessions() -> None:
    """Close all cached MCP sessions.  Called during shutdown."""
    server_ids = list(_sessions.keys())
    for sid in server_ids:
        await _evict_session(sid)
    logger.info("All MCP client sessions closed (%d)", len(server_ids))


# ─── Dynamic Pydantic model (reuse from org_tools) ───────────────────────────


def _build_pydantic_model(name: str, schema: dict[str, Any]) -> type:
    """Create a Pydantic model from an MCP tool's inputSchema."""
    from org_tools import _build_pydantic_model as _build

    return _build(name, schema)


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
                    combined = combined[:_MAX_RESPONSE_BYTES]
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
    """Build LangChain tools from all active MCP servers for an org.

    Returns ``(tools_list, tools_by_name_dict)``.
    Tools whose prefixed names collide with a global registry tool are skipped.
    Results are cached with a TTL.
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
