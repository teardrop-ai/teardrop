# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""MCP Streamable-HTTP session pool with SSRF-pinned outbound connections."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from typing import Any

from mcp_client.base import (
    OrgMcpServer,
    _decrypt_token,
    _get_pool,
    _record_event,
    logger,
)
from teardrop.config import get_settings

_sessions: dict[str, tuple[Any, AsyncExitStack, float]] = {}
# Key: server_id → (ClientSession, exit_stack, expires_at_monotonic)


def _ssrf_safe_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: Any = None,
    auth: Any = None,
) -> Any:
    """httpx client factory for ``streamablehttp_client`` that pins SSRF-safe IPs.

    Re-validates and pins every connection (including redirects) to an IP that
    passed the SSRF blocklist, closing the DNS-rebinding TOCTOU window for
    outbound MCP traffic.
    """
    import httpx

    from tools.definitions.http_fetch import make_ssrf_safe_httpx_transport

    kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "transport": make_ssrf_safe_httpx_transport(),
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


class _SessionPool:
    """Manages cached Streamable-HTTP MCP sessions per server."""

    async def _build_auth_headers(self, server: OrgMcpServer) -> dict[str, str]:
        headers: dict[str, str] = {}
        if not server.has_auth:
            return headers

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
        return headers

    async def get_or_create(self, server: OrgMcpServer) -> Any:
        """Return a cached MCP ClientSession or create a new one."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        settings = get_settings()
        now = time.monotonic()

        cached = _sessions.get(server.id)
        if cached is not None:
            session, _, expires_at = cached
            if now < expires_at:
                return session
            await self.evict(server.id)

        headers = await self._build_auth_headers(server)
        exit_stack = AsyncExitStack()
        try:
            transport = await exit_stack.enter_async_context(
                streamablehttp_client(
                    url=server.url,
                    headers=headers or None,
                    timeout=float(settings.mcp_client_connect_timeout_seconds),
                    httpx_client_factory=_ssrf_safe_mcp_http_client,
                )
            )
            read_stream, write_stream, _ = transport
            session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
            # Wait for initialization. We use a generous timeout here to avoid flaky CI connections.
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

    async def evict(self, server_id: str) -> None:
        """Close and remove a cached session."""
        entry = _sessions.pop(server_id, None)
        if entry is not None:
            _, exit_stack, _ = entry
            try:
                await exit_stack.aclose()
            except Exception:
                logger.debug("Error closing MCP session %s", server_id, exc_info=True)

    async def close_all(self) -> None:
        """Close all cached sessions."""
        server_ids = list(_sessions.keys())
        for sid in server_ids:
            await self.evict(sid)
        logger.info("All MCP client sessions closed (%d)", len(server_ids))


_session_pool: _SessionPool | None = None


def _get_session_pool() -> _SessionPool:
    global _session_pool
    if _session_pool is None:
        _session_pool = _SessionPool()
    return _session_pool


async def _get_or_create_session(server: OrgMcpServer) -> Any:
    """Return a cached MCP ClientSession or create a new one.

    Uses Streamable HTTP transport with optional auth headers.
    """
    return await _get_session_pool().get_or_create(server)


async def _evict_session(server_id: str) -> None:
    """Close and remove a cached session."""
    await _get_session_pool().evict(server_id)


async def _close_all_sessions() -> None:
    """Close all cached MCP sessions.  Called during shutdown."""
    await _get_session_pool().close_all()
