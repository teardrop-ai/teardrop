# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Internal marketplace tool price caches and lookups."""

from __future__ import annotations

from marketplace.context import _get_pool
from teardrop.cache import TTLCache

_PLATFORM_TOOL_PRICE_TTL_SECONDS = 60
_ORG_TOOL_PRICE_TTL_SECONDS = 60

_platform_tool_caches: dict[str, TTLCache[int | None]] = {}
_org_tool_price_caches: dict[str, TTLCache[int | None]] = {}


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
