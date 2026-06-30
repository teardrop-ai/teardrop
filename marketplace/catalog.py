# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace author-config, catalog queries, and tool price caches."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from marketplace._catalog_pricing import (
    get_org_tool_price_by_qualified_name as get_org_tool_price_by_qualified_name,
)
from marketplace._catalog_pricing import (
    get_platform_tool_price as get_platform_tool_price,
)
from marketplace.context import _get_pool
from marketplace.models import AuthorConfig, MarketplaceCategory, MarketplaceTool, normalize_eip55_address

_CATALOG_SORT_COLUMNS = {
    "name": "qualified_name ASC",
    "price_asc": "base_price_usdc ASC, qualified_name ASC",
    "price_desc": "base_price_usdc DESC, qualified_name ASC",
    "popularity": "total_calls DESC, qualified_name ASC",
}

_VALID_CATEGORIES = {"", "defi", "search", "data", "communication", "utility"}

# Sentinel used to distinguish "filter to platform only" from "no filter".
PLATFORM_SLUG = "platform"


async def set_author_config(
    org_id: str,
    *,
    settlement_wallet: str,
) -> AuthorConfig:
    """Create or update the author's marketplace configuration."""
    pool = _get_pool()

    normalized_wallet, wallet_error = normalize_eip55_address(settlement_wallet)
    if wallet_error is not None:
        raise ValueError(wallet_error)

    now = datetime.now(timezone.utc)

    await pool.execute(
        """
        INSERT INTO tool_author_config
            (org_id, settlement_wallet, created_at, updated_at)
        VALUES ($1, $2, $3, $3)
        ON CONFLICT (org_id) DO UPDATE
            SET settlement_wallet = EXCLUDED.settlement_wallet,
                updated_at = EXCLUDED.updated_at
        """,
        org_id,
        normalized_wallet,
        now,
    )

    return AuthorConfig(
        org_id=org_id,
        settlement_wallet=normalized_wallet,
        created_at=now,
        updated_at=now,
    )


async def get_author_config(org_id: str) -> AuthorConfig | None:
    """Return the author config for an org, or None if not configured."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT org_id, settlement_wallet, created_at, updated_at FROM tool_author_config WHERE org_id = $1",
        org_id,
    )
    if row is None:
        return None
    return AuthorConfig(**dict(row))


async def get_marketplace_catalog(
    tool_overrides: dict[str, int] | None = None,
    default_tool_cost: int = 0,
    *,
    org_slug: str | None = None,
    category: MarketplaceCategory | None = None,
    sort: str = "name",
    limit: int = 100,
    cursor: str | None = None,
    tool_name: str | None = None,
    q: str | None = None,
) -> list[MarketplaceTool]:
    """Return published marketplace tools with optional filtering and sorting."""
    if sort not in _CATALOG_SORT_COLUMNS:
        raise ValueError(f"Invalid sort '{sort}'. Allowed: {', '.join(_CATALOG_SORT_COLUMNS)}")
    if category is not None and category not in _VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Allowed: {', '.join(sorted(_VALID_CATEGORIES))}")

    limit = min(max(1, limit), 200)

    if tool_overrides is None:
        tool_overrides = {}

    if q is not None:
        q = q.strip()
        if not q:
            q = None

    import base64 as _b64
    import json as _json

    pool = _get_pool()

    cursor_sort_key: Any = None
    cursor_name: str | None = None
    if cursor:
        try:
            cursor_data = _json.loads(_b64.b64decode(cursor).decode())
            cursor_sort_key = cursor_data.get("sort_key")
            cursor_name = cursor_data.get("name")
        except Exception:
            pass

    params: list[Any] = []
    selects: list[str] = []

    def _add_param(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    search_ref: str | None = None
    if q is not None:
        escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        search_ref = _add_param(f"%{escaped_q}%")

    include_community = org_slug != PLATFORM_SLUG
    include_platform = org_slug is None or org_slug == PLATFORM_SLUG

    if include_community:
        where_clauses = ["t.publish_as_mcp = TRUE", "t.is_active = TRUE"]
        if org_slug:
            where_clauses.append(f"o.slug = {_add_param(org_slug)}")
        if category is not None:
            where_clauses.append(f"COALESCE(t.category, '') = {_add_param(category)}")
        if tool_name is not None:
            where_clauses.append(f"t.name = {_add_param(tool_name)}")
        if search_ref is not None:
            where_clauses.append(
                "("
                f"t.name ILIKE {search_ref} ESCAPE '\\' OR "
                f"t.description ILIKE {search_ref} ESCAPE '\\' OR "
                f"t.marketplace_description ILIKE {search_ref} ESCAPE '\\' OR "
                f"o.name ILIKE {search_ref} ESCAPE '\\' OR "
                f"o.slug ILIKE {search_ref} ESCAPE '\\'"
                ")"
            )
        where_sql = " AND ".join(where_clauses)
        selects.append(
            f"""
            SELECT
                t.id AS tool_id,
                t.name AS name,
                (o.slug || '/' || t.name) AS qualified_name,
                t.name AS display_name,
                t.description AS description,
                COALESCE(NULLIF(t.marketplace_description, ''), t.description) AS marketplace_description,
                t.input_schema AS input_schema,
                t.base_price_usdc AS base_price_usdc,
                o.name AS author_org_name,
                o.slug AS author_org_slug,
                'community' AS tool_type,
                COALESCE(t.category, '') AS category,
                COALESCE(s.total_calls, 0)::BIGINT AS total_calls
            FROM org_tools t
            JOIN orgs o ON o.id = t.org_id
            LEFT JOIN marketplace_tool_call_stats s ON s.qualified_tool_name = (o.slug || '/' || t.name)
            WHERE {where_sql}
            """
        )

    if include_platform:
        where_clauses = ["p.is_active = TRUE"]
        if category is not None:
            where_clauses.append(f"COALESCE(p.category, '') = {_add_param(category)}")
        if tool_name is not None:
            where_clauses.append(f"p.tool_name = {_add_param(tool_name)}")
        if search_ref is not None:
            where_clauses.append(
                "("
                f"p.tool_name ILIKE {search_ref} ESCAPE '\\' OR "
                f"p.display_name ILIKE {search_ref} ESCAPE '\\' OR "
                f"p.description ILIKE {search_ref} ESCAPE '\\' OR "
                f"'Teardrop' ILIKE {search_ref} ESCAPE '\\' OR "
                f"'{PLATFORM_SLUG}' ILIKE {search_ref} ESCAPE '\\'"
                ")"
            )
        where_sql = " AND ".join(where_clauses)
        selects.append(
            f"""
            SELECT
                NULL::TEXT AS tool_id,
                p.tool_name AS name,
                ('{PLATFORM_SLUG}/' || p.tool_name) AS qualified_name,
                p.display_name AS display_name,
                p.description AS description,
                p.description AS marketplace_description,
                '{{}}'::JSONB AS input_schema,
                p.base_price_usdc AS base_price_usdc,
                'Teardrop' AS author_org_name,
                '{PLATFORM_SLUG}' AS author_org_slug,
                'platform' AS tool_type,
                COALESCE(p.category, '') AS category,
                COALESCE(s.total_calls, 0)::BIGINT AS total_calls
            FROM marketplace_platform_tools p
            LEFT JOIN marketplace_tool_call_stats s ON s.qualified_tool_name = ('{PLATFORM_SLUG}/' || p.tool_name)
            WHERE {where_sql}
            """
        )

    cursor_clause = ""
    if cursor_sort_key is not None and cursor_name is not None:
        if sort == "name":
            cursor_clause = f"WHERE qualified_name > {_add_param(str(cursor_name))}"
        elif sort == "price_asc":
            key_ref = _add_param(int(cursor_sort_key))
            name_ref = _add_param(str(cursor_name))
            cursor_clause = (
                f"WHERE (base_price_usdc > {key_ref} OR (base_price_usdc = {key_ref} AND qualified_name > {name_ref}))"
            )
        elif sort == "price_desc":
            key_ref = _add_param(int(cursor_sort_key))
            name_ref = _add_param(str(cursor_name))
            cursor_clause = (
                f"WHERE (base_price_usdc < {key_ref} OR (base_price_usdc = {key_ref} AND qualified_name > {name_ref}))"
            )
        elif sort == "popularity":
            key_ref = _add_param(int(cursor_sort_key))
            name_ref = _add_param(str(cursor_name))
            cursor_clause = f"WHERE (total_calls < {key_ref} OR (total_calls = {key_ref} AND qualified_name > {name_ref}))"

    limit_ref = _add_param(limit)
    union_sql = " UNION ALL ".join(selects) if selects else "SELECT NULL WHERE FALSE"
    rows = await pool.fetch(
        f"""
        WITH all_tools AS (
            {union_sql}
        )
        SELECT *
        FROM all_tools
        {cursor_clause}
        ORDER BY {_CATALOG_SORT_COLUMNS[sort]}
        LIMIT {limit_ref}
        """,
        *params,
    )

    catalog: list[MarketplaceTool] = []
    for row in rows:
        raw_schema = _row_get(row, "input_schema", {})
        if isinstance(raw_schema, str):
            raw_schema = _json.loads(raw_schema)
        if not raw_schema and _row_get(row, "tool_type", "community") == "platform":
            raw_schema = _platform_input_schema(str(row["name"]))

        qualified = str(row["qualified_name"])
        name = str(row["name"])
        base_price = int(_row_get(row, "base_price_usdc", 0) or 0)
        cost = tool_overrides.get(qualified, tool_overrides.get(name, base_price or default_tool_cost))
        total_calls = int(_row_get(row, "total_calls", 0) or 0)
        health_status = await _tool_health_status(_row_get(row, "tool_id"), str(_row_get(row, "tool_type", "community")))

        sort_key: Any
        if sort in {"price_asc", "price_desc"}:
            sort_key = base_price
        elif sort == "popularity":
            sort_key = total_calls
        else:
            sort_key = qualified

        catalog.append(
            MarketplaceTool(
                name=name,
                qualified_name=qualified,
                display_name=str(_row_get(row, "display_name", name) or name),
                description=str(_row_get(row, "description", "")),
                marketplace_description=str(_row_get(row, "marketplace_description", "") or _row_get(row, "description", "")),
                input_schema=raw_schema or {},
                cost_usdc=cost,
                author_org_name=str(_row_get(row, "author_org_name", "")),
                author_org_slug=str(_row_get(row, "author_org_slug", "")),
                tool_type=str(_row_get(row, "tool_type", "community")),
                total_calls=total_calls,
                health_status=health_status,
                is_healthy=health_status == "healthy",
                category=str(_row_get(row, "category", "") or ""),
                sort_key=sort_key,
            )
        )

    return catalog


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        get = getattr(row, "get", None)
        return get(key, default) if callable(get) else default


def _platform_input_schema(tool_name: str) -> dict[str, Any]:
    try:
        from tools import registry

        tool_def = registry.get(tool_name)
        if tool_def is not None and hasattr(tool_def.input_schema, "model_json_schema"):
            return tool_def.input_schema.model_json_schema()
    except Exception:
        pass
    return {}


async def _tool_health_status(tool_id: Any, tool_type: str) -> str:
    if tool_type != "community" or not tool_id:
        return "healthy"
    try:
        from tools.health import is_breaker_tripped

        return "unavailable" if await is_breaker_tripped(str(tool_id)) else "healthy"
    except Exception:
        return "healthy"


async def get_marketplace_catalog_tool(
    tool_name: str,
    org_slug: str,
    tool_overrides: dict[str, int] | None = None,
    default_tool_cost: int = 0,
) -> MarketplaceTool | None:
    """Return a single public catalog tool, or None when unpublished/missing."""
    tools = await get_marketplace_catalog(
        tool_overrides,
        default_tool_cost,
        org_slug=org_slug,
        sort="name",
        limit=1,
        tool_name=tool_name,
    )
    return tools[0] if tools else None


async def get_marketplace_author_summary(org_slug: str) -> dict[str, Any] | None:
    """Return public aggregate metadata for a marketplace author/profile."""
    pool = _get_pool()
    if org_slug == PLATFORM_SLUG:
        row = await pool.fetchrow(
            """
            SELECT COUNT(*)::INT AS tool_count,
                   COALESCE(SUM(s.total_calls), 0)::BIGINT AS total_calls
            FROM marketplace_platform_tools p
            LEFT JOIN marketplace_tool_call_stats s ON s.qualified_tool_name = ('platform/' || p.tool_name)
            WHERE p.is_active = TRUE
            """
        )
        return {
            "org_slug": PLATFORM_SLUG,
            "org_name": "Teardrop",
            "tool_count": int(_row_get(row, "tool_count", 0) or 0),
            "total_calls": int(_row_get(row, "total_calls", 0) or 0),
        }

    row = await pool.fetchrow(
        """
        SELECT o.name AS org_name,
               o.slug AS org_slug,
               COUNT(t.id)::INT AS tool_count,
               COALESCE(SUM(s.total_calls), 0)::BIGINT AS total_calls
        FROM orgs o
        LEFT JOIN org_tools t
            ON t.org_id = o.id
           AND t.publish_as_mcp = TRUE
           AND t.is_active = TRUE
        LEFT JOIN marketplace_tool_call_stats s ON s.qualified_tool_name = (o.slug || '/' || t.name)
        WHERE o.slug = $1
        GROUP BY o.id, o.name, o.slug
        """,
        org_slug,
    )
    if row is None:
        return None
    return {
        "org_slug": str(row["org_slug"]),
        "org_name": str(row["org_name"]),
        "tool_count": int(_row_get(row, "tool_count", 0) or 0),
        "total_calls": int(_row_get(row, "total_calls", 0) or 0),
    }


def _build_catalog_cursor(tool: MarketplaceTool, sort: str) -> str:
    """Build an opaque pagination cursor for the given tool and sort order."""
    import base64 as _b64
    import json as _json

    if tool.sort_key is not None:
        data = {"sort_key": tool.sort_key, "name": tool.qualified_name}
    elif sort == "price_asc" or sort == "price_desc":
        data = {"sort_key": tool.cost_usdc, "name": tool.qualified_name}
    elif sort == "popularity":
        data = {"sort_key": tool.total_calls, "name": tool.qualified_name}
    else:
        data = {"sort_key": tool.qualified_name, "name": tool.qualified_name}
    return _b64.b64encode(_json.dumps(data).encode()).decode()


async def get_marketplace_tool_by_name(
    tool_name: str,
    org_slug: str,
) -> dict[str, Any] | None:
    """Look up a published tool by name and org slug."""
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT t.*, o.slug AS org_slug, o.name AS org_name
        FROM org_tools t
        JOIN orgs o ON o.id = t.org_id
        WHERE t.name = $1 AND o.slug = $2 AND t.publish_as_mcp = TRUE AND t.is_active = TRUE
        """,
        tool_name,
        org_slug,
    )
    if row is None:
        return None
    return dict(row)
