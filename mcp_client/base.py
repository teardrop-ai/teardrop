# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Foundational state and helpers for the per-org MCP client.

Holds the shared pool reference, constants, the ``OrgMcpServer`` model, token
encryption helpers, audit logging, and row mapping. Kept dependency-free of the
other ``mcp_client`` submodules (only a lazy import for session teardown) to
avoid circular-import cycles.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

import asyncpg
from pydantic import BaseModel

from shared.audit import insert_event_row
from shared.db_pool import bind_pool, require_pool, unbind_pool
from tools.shared import decrypt_header_value, encrypt_header_value

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_MAX_RESPONSE_BYTES = 50 * 1024  # 50 KB response cap per MCP tool call
_NAME_SEPARATOR = "__"  # server_name__tool_name
_POOL_SCOPE = "mcp_client"
_MCP_EVENT_INSERT_SQL = (
    "INSERT INTO org_mcp_server_events"
    " (id, org_id, server_id, server_name, event_type, detail, actor_id)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7)"
)

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
    _pool = bind_pool(_POOL_SCOPE, pool)
    logger.info("MCP client DB ready")


async def close_mcp_client_db() -> None:
    """Release the pool reference and close all cached MCP sessions."""
    global _pool
    from mcp_client.session import _close_all_sessions

    await _close_all_sessions()
    if _pool is not None:
        _pool = None
        unbind_pool(_POOL_SCOPE)
        logger.info("MCP client DB reference released")


def _get_pool() -> asyncpg.Pool:
    return require_pool(
        _POOL_SCOPE,
        _pool,
        "MCP client DB not initialised — call init_mcp_client_db() first",
    )


# ─── Encryption (shared helpers) ──────────────────────────────────────────────


def _encrypt_token(value: str) -> str:
    """Encrypt an auth token for at-rest storage."""
    return encrypt_header_value(value)


def _decrypt_token(encrypted: str) -> str:
    """Decrypt a stored auth token."""
    return decrypt_header_value(encrypted)


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
        await insert_event_row(
            pool,
            insert_sql=_MCP_EVENT_INSERT_SQL,
            values=(
                org_id,
                server_id,
                server_name,
                event_type,
                detail,
                actor_id,
            ),
        )
    except Exception:
        logger.warning("Failed to record MCP server event", exc_info=True)


# ─── Row mapping ──────────────────────────────────────────────────────────────


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
