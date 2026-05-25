# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace author-config, catalog queries, and tool price caches."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from marketplace.context import _get_pool
from marketplace.models import AuthorConfig, MarketplaceTool, validate_eip55_address
from teardrop.cache import TTLCache

_CATALOG_SORT_COLUMNS = {
    "name": "t.name ASC",
    "price_asc": "t.base_price_usdc ASC, t.name ASC",
    "price_desc": "t.base_price_usdc DESC, t.name ASC",
}

# Sentinel used to distinguish "filter to platform only" from "no filter".
PLATFORM_SLUG = "platform"

_PLATFORM_TOOL_PRICE_TTL_SECONDS = 60
_ORG_TOOL_PRICE_TTL_SECONDS = 60

_platform_tool_caches: dict[str, TTLCache[int | None]] = {}
_org_tool_price_caches: dict[str, TTLCache[int | None]] = {}


async def set_author_config(
    org_id: str,
    *,
    settlement_wallet: str,
) -> AuthorConfig:
    """Create or update the author's marketplace configuration."""
    pool = _get_pool()

    wallet_error = validate_eip55_address(settlement_wallet)
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
        settlement_wallet,
        now,
    )

    return AuthorConfig(
        org_id=org_id,
        settlement_wallet=settlement_wallet,
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
    sort: str = "name",
    limit: int = 100,
    cursor: str | None = None,
) -> list[MarketplaceTool]:
    """Return published marketplace tools with optional filtering and sorting."""
    if sort not in _CATALOG_SORT_COLUMNS:
        raise ValueError(f"Invalid sort '{sort}'. Allowed: {', '.join(_CATALOG_SORT_COLUMNS)}")

    limit = min(max(1, limit), 200)

    if tool_overrides is None:
        tool_overrides = {}

    import base64 as _b64
    import json as _json

    pool = _get_pool()
    catalog: list[MarketplaceTool] = []

    cursor_sort_key: Any = None
    cursor_name: str | None = None
    if cursor:
        try:
            cursor_data = _json.loads(_b64.b64decode(cursor).decode())
            cursor_sort_key = cursor_data.get("sort_key")
            cursor_name = cursor_data.get("name")
        except Exception:
            pass

    if org_slug != PLATFORM_SLUG:
        order_col = _CATALOG_SORT_COLUMNS[sort]

        where_clauses = ["t.publish_as_mcp = TRUE", "t.is_active = TRUE"]
        params: list[Any] = []

        if org_slug:
            params.append(org_slug)
            where_clauses.append(f"o.slug = ${len(params)}")

        if cursor_sort_key is not None and cursor_name is not None:
            if sort == "name":
                params.append(cursor_name)
                where_clauses.append(f"t.name > ${len(params)}")
            elif sort == "price_asc":
                params.append(cursor_sort_key)
                params.append(cursor_name)
                where_clauses.append(
                    f"(t.base_price_usdc > ${len(params) - 1} OR "
                    f"(t.base_price_usdc = ${len(params) - 1} AND t.name > ${len(params)}))"
                )
            elif sort == "price_desc":
                params.append(cursor_sort_key)
                params.append(cursor_name)
                where_clauses.append(
                    f"(t.base_price_usdc < ${len(params) - 1} OR "
                    f"(t.base_price_usdc = ${len(params) - 1} AND t.name > ${len(params)}))"
                )

        params.append(limit)
        where_sql = " AND ".join(where_clauses)

        rows = await pool.fetch(
            f"""
            SELECT t.name, t.description, t.marketplace_description, t.input_schema,
                   t.base_price_usdc,
                   o.name AS org_name, o.slug AS org_slug
            FROM org_tools t
            JOIN orgs o ON o.id = t.org_id
            WHERE {where_sql}
            ORDER BY {order_col}
            LIMIT ${len(params)}
            """,
            *params,
        )

        for r in rows:
            raw_schema = r["input_schema"]
            if isinstance(raw_schema, str):
                raw_schema = _json.loads(raw_schema)

            qualified = f"{r['org_slug']}/{r['name']}"
            author_price = r.get("base_price_usdc", 0)
            cost = tool_overrides.get(qualified, tool_overrides.get(r["name"], author_price or default_tool_cost))

            catalog.append(
                MarketplaceTool(
                    name=r["name"],
                    qualified_name=qualified,
                    display_name=r["name"],
                    description=r["description"],
                    marketplace_description=r["marketplace_description"] or r["description"],
                    input_schema=raw_schema,
                    cost_usdc=cost,
                    author_org_name=r["org_name"],
                    author_org_slug=r["org_slug"],
                    tool_type="community",
                )
            )

    if org_slug is None or org_slug == PLATFORM_SLUG:
        platform_limit_clause = f"LIMIT {limit}" if org_slug == PLATFORM_SLUG else ""
        platform_rows = await pool.fetch(
            f"""
            SELECT tool_name, display_name, description, base_price_usdc
            FROM marketplace_platform_tools
            WHERE is_active = TRUE
            ORDER BY tool_name
            {platform_limit_clause}
            """
        )
        for pr in platform_rows:
            name = pr["tool_name"]
            cost = tool_overrides.get(name, pr["base_price_usdc"] or default_tool_cost)
            catalog.append(
                MarketplaceTool(
                    name=name,
                    qualified_name=f"{PLATFORM_SLUG}/{name}",
                    display_name=pr["display_name"],
                    description=pr["description"],
                    marketplace_description=pr["description"],
                    input_schema={},
                    cost_usdc=cost,
                    author_org_name="Teardrop",
                    author_org_slug=PLATFORM_SLUG,
                    tool_type="platform",
                )
            )

    return catalog


def _build_catalog_cursor(tool: MarketplaceTool, sort: str) -> str:
    """Build an opaque pagination cursor for the given tool and sort order."""
    import base64 as _b64
    import json as _json

    if sort == "price_asc" or sort == "price_desc":
        data = {"sort_key": tool.cost_usdc, "name": tool.name}
    else:
        data = {"sort_key": tool.name, "name": tool.name}
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


async def _load_platform_tool_price(tool_name: str) -> int | None:
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT base_price_usdc FROM marketplace_platform_tools WHERE tool_name = $1 AND is_active = TRUE",
        tool_name,
    )
    if row is None:
        return None
    return int(row["base_price_usdc"])


def _get_platform_tool_cache(tool_name: str) -> TTLCache[int | None]:
    if tool_name not in _platform_tool_caches:
        _platform_tool_caches[tool_name] = TTLCache(
            name=f"platform_tool_price:{tool_name}",
            redis_key=f"teardrop:platform_tool_price:{tool_name}",
            ttl_seconds_fn=lambda: _PLATFORM_TOOL_PRICE_TTL_SECONDS,
            loader=lambda: _load_platform_tool_price(tool_name),
            serialize=lambda v: str(v),
            deserialize=lambda raw: int(raw),
            stale_default=None,
        )
    return _platform_tool_caches[tool_name]


async def _load_org_tool_price(qualified_name: str) -> int | None:
    org_slug, tool_name = qualified_name.split("/", 1)
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT t.base_price_usdc
        FROM org_tools t
        JOIN orgs o ON o.id = t.org_id
        WHERE t.name = $1
          AND o.slug = $2
          AND t.publish_as_mcp = TRUE
          AND t.is_active = TRUE
        """,
        tool_name,
        org_slug,
    )
    if row is None:
        return None
    return int(row["base_price_usdc"])


def _get_org_tool_price_cache(qualified_name: str) -> TTLCache[int | None]:
    if qualified_name not in _org_tool_price_caches:
        _org_tool_price_caches[qualified_name] = TTLCache(
            name=f"org_tool_price:{qualified_name}",
            redis_key=f"teardrop:org_tool_price:{qualified_name}",
            ttl_seconds_fn=lambda: _ORG_TOOL_PRICE_TTL_SECONDS,
            loader=lambda: _load_org_tool_price(qualified_name),
            serialize=lambda v: str(v),
            deserialize=lambda raw: int(raw),
            stale_default=None,
        )
    return _org_tool_price_caches[qualified_name]


async def _invalidate_platform_tool_cache() -> None:
    """Drop platform tool price caches (in-process + Redis)."""
    for cache in _platform_tool_caches.values():
        await cache.invalidate()
    _platform_tool_caches.clear()


async def _invalidate_all_org_tool_price_cache() -> None:
    """Drop qualified org tool price caches (in-process + Redis)."""
    for cache in _org_tool_price_caches.values():
        await cache.invalidate()
    _org_tool_price_caches.clear()


async def get_platform_tool_price(tool_name: str) -> int | None:
    """Return base_price_usdc for a platform-owned marketplace tool, or None."""
    return await _get_platform_tool_cache(tool_name).get()


async def get_org_tool_price_by_qualified_name(qualified_name: str) -> int | None:
    """Return base_price_usdc for a qualified marketplace tool, or None."""
    if "/" not in qualified_name:
        return None

    org_slug, tool_name = qualified_name.split("/", 1)
    if not org_slug or not tool_name:
        return None

    return await _get_org_tool_price_cache(qualified_name).get()
