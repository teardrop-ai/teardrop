# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""CRUD operations for per-org MCP server configs."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from mcp_client.base import (
    OrgMcpServer,
    _encrypt_token,
    _get_pool,
    _record_event,
    _row_to_model,
)
from mcp_client.cache import invalidate_mcp_cache
from mcp_client.session import _evict_session
from teardrop.config import get_settings


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
