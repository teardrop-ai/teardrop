# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Foundational state and helpers for the org-tool registry.

Holds the shared module-level pool reference, constants, the ``OrgTool`` model,
header encryption helpers, audit logging, and row mapping. Other ``org_tools``
submodules build on this foundation; keeping it dependency-free (no intra-package
imports) avoids circular-import cycles.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg
from pydantic import BaseModel

from shared.audit import insert_event_row
from shared.db_pool import bind_pool, require_pool, unbind_pool
from tools.shared import (
    decrypt_header_value,
    encrypt_header_value,
)

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_MAX_RESPONSE_BYTES = 50 * 1024  # 50 KB webhook response cap
_POOL_SCOPE = "org_tools"
_ORG_TOOL_EVENT_INSERT_SQL = (
    "INSERT INTO org_tool_events"
    " (id, org_id, tool_id, tool_name, event_type, actor_id, detail)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7)"
)
_VALID_MARKETPLACE_CATEGORIES = {"", "defi", "search", "data", "communication", "utility"}

# ─── Models ───────────────────────────────────────────────────────────────────


class OrgTool(BaseModel):
    """Public representation of a custom tool (never contains raw auth values)."""

    id: str
    org_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    webhook_url: str | None = None
    webhook_method: str
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    has_auth: bool
    timeout_seconds: int
    is_active: bool
    publish_as_mcp: bool = False
    marketplace_description: str = ""
    category: str = ""
    base_price_usdc: int = 0
    schema_hash: str = ""
    last_schema_changed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ─── Database pool ────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_org_tools_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference.  Called during app lifespan startup."""
    global _pool
    _pool = bind_pool(_POOL_SCOPE, pool)
    logger.info("Org tools DB ready")


async def close_org_tools_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        unbind_pool(_POOL_SCOPE)
        logger.info("Org tools DB reference released")


def _get_pool() -> asyncpg.Pool:
    return require_pool(
        _POOL_SCOPE,
        _pool,
        "Org tools DB not initialised — call init_org_tools_db() first",
    )


# ─── Encryption ───────────────────────────────────────────────────────────────


def _encrypt_header(value: str) -> str:
    """Encrypt a webhook auth header value.  Returns base64 Fernet token."""
    return encrypt_header_value(value)


def _decrypt_header(encrypted: str) -> str:
    """Decrypt a webhook auth header value."""
    return decrypt_header_value(encrypted)


# ─── Audit logging ────────────────────────────────────────────────────────────


async def _record_event(
    org_id: str,
    tool_id: str,
    tool_name: str,
    event_type: str,
    actor_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Insert an immutable audit event.  Best-effort — errors logged, never raised."""
    try:
        pool = _get_pool()
        await insert_event_row(
            pool,
            insert_sql=_ORG_TOOL_EVENT_INSERT_SQL,
            values=(
                org_id,
                tool_id,
                tool_name,
                event_type,
                actor_id,
                json.dumps(detail or {}),
            ),
        )
    except Exception:
        logger.warning("Failed to record org tool event", exc_info=True)


# ─── Row mapping ──────────────────────────────────────────────────────────────


def _row_to_org_tool(row: asyncpg.Record) -> OrgTool:
    """Map a DB row to an OrgTool model."""
    schema_raw = row["input_schema"]
    if isinstance(schema_raw, str):
        schema_raw = json.loads(schema_raw)
    output_schema_raw = row.get("output_schema")
    if isinstance(output_schema_raw, str):
        output_schema_raw = json.loads(output_schema_raw)
    return OrgTool(
        id=row["id"],
        org_id=row["org_id"],
        name=row["name"],
        description=row["description"],
        input_schema=schema_raw,
        output_schema=output_schema_raw,
        webhook_url=row["webhook_url"],
        webhook_method=row["webhook_method"],
        mcp_server_id=row.get("mcp_server_id"),
        mcp_tool_name=row.get("mcp_tool_name"),
        has_auth=row["auth_header_name"] is not None,
        timeout_seconds=row["timeout_seconds"],
        is_active=row["is_active"],
        publish_as_mcp=row.get("publish_as_mcp", False),
        marketplace_description=row.get("marketplace_description", ""),
        category=row.get("category", ""),
        base_price_usdc=row.get("base_price_usdc", 0),
        schema_hash=row.get("schema_hash") or "",
        last_schema_changed_at=row.get("last_schema_changed_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
