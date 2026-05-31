# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""CRUD operations for the org-tool registry.

create / get / list / update / delete custom webhook tools, with per-org quotas,
unique-name enforcement, marketplace publish validation, and audit + cache
invalidation side-effects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from org_tools.base import (
    _VALID_MARKETPLACE_CATEGORIES,
    OrgTool,
    _encrypt_header,
    _get_pool,
    _record_event,
    _row_to_org_tool,
)
from org_tools.cache import invalidate_marketplace_cache, invalidate_org_tools_cache
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


async def create_org_tool(
    org_id: str,
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    webhook_url: str,
    auth_header_name: str | None,
    auth_header_value: str | None,
    timeout_seconds: int,
    actor_id: str,
    output_schema: dict[str, Any] | None = None,
    publish_as_mcp: bool = False,
    marketplace_description: str = "",
    category: str = "",
    base_price_usdc: int = 0,
) -> OrgTool:
    """Insert a new custom tool.  Raises on duplicate name or quota exceeded."""
    pool = _get_pool()
    settings = get_settings()

    # Quota check
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM org_tools WHERE org_id = $1 AND is_active = TRUE",
        org_id,
    )
    if count >= settings.max_org_tools:
        raise ValueError(f"Organisation tool limit reached ({settings.max_org_tools})")

    tool_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    auth_enc: str | None = None
    if auth_header_value:
        auth_enc = _encrypt_header(auth_header_value)

    # Validate: publishing requires author config
    if publish_as_mcp:
        from marketplace import get_author_config

        config = await get_author_config(org_id)
        if config is None:
            raise ValueError(
                "Cannot publish tool to marketplace — register a settlement wallet first via POST /marketplace/author-config"
            )

    if category not in _VALID_MARKETPLACE_CATEGORIES:
        raise ValueError("Invalid marketplace category")

    try:
        await pool.execute(
            "INSERT INTO org_tools"
            " (id, org_id, name, description, input_schema, output_schema,"
            "  webhook_url, webhook_method,"
            "  auth_header_name, auth_header_enc,"
            "  timeout_seconds, is_active,"
            "  publish_as_mcp, marketplace_description, category, base_price_usdc,"
            "  created_at, updated_at, last_schema_changed_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, TRUE, $12, $13, $14, $15, $16, $16, $16)",
            tool_id,
            org_id,
            name,
            description,
            json.dumps(input_schema),
            json.dumps(output_schema) if output_schema is not None else None,
            webhook_url,
            "GET",
            auth_header_name,
            auth_enc,
            timeout_seconds,
            publish_as_mcp,
            marketplace_description,
            category,
            base_price_usdc,
            now,
        )
    except asyncpg.UniqueViolationError:
        raise ValueError(f"Tool '{name}' already exists for this organisation")

    await _record_event(org_id, tool_id, name, "created", actor_id)
    await invalidate_org_tools_cache(org_id)
    if publish_as_mcp:
        await invalidate_marketplace_cache()

    return OrgTool(
        id=tool_id,
        org_id=org_id,
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        webhook_url=webhook_url,
        webhook_method="GET",
        has_auth=auth_header_name is not None,
        timeout_seconds=timeout_seconds,
        is_active=True,
        publish_as_mcp=publish_as_mcp,
        marketplace_description=marketplace_description,
        category=category,
        base_price_usdc=base_price_usdc,
        last_schema_changed_at=now,
        created_at=now,
        updated_at=now,
    )


async def get_org_tool(tool_id: str, org_id: str) -> OrgTool | None:
    """Return a single tool scoped to the org, or None."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM org_tools WHERE id = $1 AND org_id = $2",
        tool_id,
        org_id,
    )
    if row is None:
        return None
    return _row_to_org_tool(row)


async def list_org_tools(org_id: str, *, active_only: bool = True) -> list[OrgTool]:
    """List all custom tools for an org."""
    pool = _get_pool()
    query = "SELECT * FROM org_tools WHERE org_id = $1"
    if active_only:
        query += " AND is_active = TRUE"
    query += " ORDER BY name"
    rows = await pool.fetch(query, org_id)
    return [_row_to_org_tool(r) for r in rows]


async def update_org_tool(
    tool_id: str,
    org_id: str,
    *,
    actor_id: str,
    description: str | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    webhook_url: str | None = None,
    auth_header_name: str | None = ...,  # type: ignore[assignment]
    auth_header_value: str | None = ...,  # type: ignore[assignment]
    timeout_seconds: int | None = None,
    is_active: bool | None = None,
    publish_as_mcp: bool | None = None,
    marketplace_description: str | None = None,
    category: str | None = None,
    base_price_usdc: int | None = None,
) -> OrgTool | None:
    """Partial-update a tool.  Returns updated OrgTool or None if not found."""
    pool = _get_pool()

    # Fetch current row to verify ownership and build update SET clause.
    row = await pool.fetchrow(
        "SELECT * FROM org_tools WHERE id = $1 AND org_id = $2",
        tool_id,
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

    if description is not None:
        _add("description", description)
    if input_schema is not None:
        _add("input_schema", json.dumps(input_schema))
        _add("last_schema_changed_at", datetime.now(timezone.utc))
    if output_schema is not None:
        _add("output_schema", json.dumps(output_schema))
    if webhook_url is not None:
        _add("webhook_url", webhook_url)
    if timeout_seconds is not None:
        _add("timeout_seconds", timeout_seconds)
    if is_active is not None:
        _add("is_active", is_active)
    if publish_as_mcp is not None:
        # Validate: publishing requires author config
        if publish_as_mcp:
            from marketplace import get_author_config

            config = await get_author_config(org_id)
            if config is None:
                raise ValueError("Cannot publish tool to marketplace — register a settlement wallet first")
        _add("publish_as_mcp", publish_as_mcp)
    if marketplace_description is not None:
        _add("marketplace_description", marketplace_description)
    if category is not None:
        if category not in _VALID_MARKETPLACE_CATEGORIES:
            raise ValueError("Invalid marketplace category")
        _add("category", category)
    if base_price_usdc is not None:
        _add("base_price_usdc", base_price_usdc)

    # Handle auth header updates (sentinel ... means "not provided")
    if auth_header_name is not ...:
        _add("auth_header_name", auth_header_name)
    if auth_header_value is not ...:
        if auth_header_value is not None:
            _add("auth_header_enc", _encrypt_header(auth_header_value))
        else:
            _add("auth_header_enc", None)

    if not sets:
        return _row_to_org_tool(row)

    _add("updated_at", datetime.now(timezone.utc))
    params.append(tool_id)
    params.append(org_id)

    query = f"UPDATE org_tools SET {', '.join(sets)} WHERE id = ${idx} AND org_id = ${idx + 1} RETURNING *"
    updated = await pool.fetchrow(query, *params)

    await _record_event(org_id, tool_id, row["name"], "updated", actor_id)
    await invalidate_org_tools_cache(org_id)
    # Invalidate marketplace/pricing caches if publication, visibility, or price changed.
    if publish_as_mcp is not None or is_active is not None or base_price_usdc is not None or category is not None:
        await invalidate_marketplace_cache()

    # Clear circuit breaker state on FALSE → TRUE transition so the tool
    # starts with a clean failure window after manual re-enable.
    if is_active is True and row["is_active"] is False:
        try:
            from tools.health import clear_breaker

            await clear_breaker(tool_id)
        except Exception:  # pragma: no cover
            logger.debug("clear_breaker failed during re-enable", exc_info=True)

    return _row_to_org_tool(updated) if updated else None


async def delete_org_tool(tool_id: str, org_id: str, *, actor_id: str) -> bool:
    """Soft-delete a tool (set is_active=False).  Returns True if found."""
    pool = _get_pool()
    result = await pool.execute(
        "UPDATE org_tools SET is_active = FALSE, updated_at = NOW() WHERE id = $1 AND org_id = $2 AND is_active = TRUE",
        tool_id,
        org_id,
    )
    deleted = result.split()[-1] != "0"  # "UPDATE N"
    if deleted:
        # Fetch name for audit
        row = await pool.fetchrow("SELECT name, publish_as_mcp FROM org_tools WHERE id = $1", tool_id)
        name = row["name"] if row else tool_id
        await _record_event(org_id, tool_id, name, "deleted", actor_id)
        await invalidate_org_tools_cache(org_id)

        # Clear breaker state so a future re-creation starts fresh.
        try:
            from tools.health import clear_breaker

            await clear_breaker(tool_id)
        except Exception:  # pragma: no cover
            logger.debug("clear_breaker failed during delete", exc_info=True)

        if row and row.get("publish_as_mcp"):
            await invalidate_marketplace_cache()
            # Deactivate all marketplace subscriptions for this tool so subscribers
            # are not silently left with a dead tool reference.
            org_row = await pool.fetchrow("SELECT slug FROM orgs WHERE id = $1", org_id)
            qualified_name: str | None = None
            if org_row:
                qualified_name = f"{org_row['slug']}/{name}"
                await pool.execute(
                    "UPDATE org_marketplace_subscriptions SET is_active = FALSE"
                    " WHERE qualified_tool_name = $1 AND is_active = TRUE",
                    qualified_name,
                )

            # Notify subscribers (fire-and-forget).
            if qualified_name is not None:
                try:
                    from marketplace import notify_subscribers_of_deactivation

                    asyncio.create_task(
                        notify_subscribers_of_deactivation(
                            qualified_name,
                            "manually removed by author",
                        )
                    )
                except Exception:  # pragma: no cover
                    logger.debug("Failed to schedule subscriber notification", exc_info=True)
    return deleted
