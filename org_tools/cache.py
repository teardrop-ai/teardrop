# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Caching for the org-tool registry.

Per-org TTL cache (Redis → in-process fallback) for active org tools, plus a
process-wide cache for the published marketplace tool list. Invalidation hooks
are called after every mutation in :mod:`org_tools.crud`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from org_tools.base import OrgTool, _get_pool, _row_to_org_tool
from teardrop.cache import TTLCache, get_redis
from teardrop.config import get_settings

logger = logging.getLogger(__name__)

# ─── Per-org TTL cache ────────────────────────────────────────────────────────

_org_tool_caches: dict[str, TTLCache[list[OrgTool]]] = {}


def _load_org_tools(org_id: str):
    """Loader for the per-org TTL cache (lazy import avoids a crud↔cache cycle)."""
    from org_tools.crud import list_org_tools

    return list_org_tools(org_id, active_only=True)


def _get_org_tool_cache(org_id: str) -> TTLCache[list[OrgTool]]:
    if org_id not in _org_tool_caches:
        _org_tool_caches[org_id] = TTLCache(
            name=f"org_tools:{org_id}",
            redis_key=f"teardrop:org_tools:{org_id}",
            ttl_seconds_fn=lambda: get_settings().org_tools_cache_ttl_seconds,
            loader=lambda: _load_org_tools(org_id),
            serialize=lambda tools: json.dumps([t.model_dump(mode="json") for t in tools]),
            deserialize=lambda raw: [OrgTool(**item) for item in json.loads(raw)],
        )
    return _org_tool_caches[org_id]


async def get_org_tools_cached(org_id: str) -> list[OrgTool]:
    """Return active org tools with a TTL cache (Redis → in-process fallback)."""
    return await _get_org_tool_cache(org_id).get() or []


async def invalidate_org_tools_cache(org_id: str) -> None:
    """Clear the cache for a specific org.  Called after any mutation."""
    await _get_org_tool_cache(org_id).invalidate()


# ─── Marketplace tools cache ─────────────────────────────────────────────────

_marketplace_cache: tuple[list[OrgTool], float] | None = None  # (tools, expiry)
_marketplace_lock: asyncio.Lock | None = None


def _get_marketplace_lock() -> asyncio.Lock:
    global _marketplace_lock
    if _marketplace_lock is None:
        _marketplace_lock = asyncio.Lock()
    return _marketplace_lock


async def list_marketplace_tools() -> list[OrgTool]:
    """Return all published marketplace tools with a TTL cache."""
    global _marketplace_cache
    settings = get_settings()
    redis = get_redis()

    # Redis path
    if redis is not None:
        try:
            cached_json = await redis.get("teardrop:marketplace:tools")
            if cached_json is not None:
                items = json.loads(cached_json)
                return [OrgTool(**item) for item in items]
        except Exception:
            logger.warning("Redis marketplace cache read failed; falling back", exc_info=True)

    # In-process TTL cache
    now = time.monotonic()
    if _marketplace_cache is not None and now < _marketplace_cache[1]:
        return _marketplace_cache[0]

    async with _get_marketplace_lock():
        if _marketplace_cache is not None and time.monotonic() < _marketplace_cache[1]:
            return _marketplace_cache[0]

        pool = _get_pool()
        rows = await pool.fetch("SELECT * FROM org_tools WHERE publish_as_mcp = TRUE AND is_active = TRUE ORDER BY name")
        tools = [_row_to_org_tool(r) for r in rows]
        ttl = settings.org_tools_cache_ttl_seconds
        _marketplace_cache = (tools, time.monotonic() + ttl)

        if (r := get_redis()) is not None:
            try:
                data = json.dumps([t.model_dump(mode="json") for t in tools])
                await r.setex("teardrop:marketplace:tools", ttl, data)
            except Exception:
                logger.warning("Redis marketplace cache write failed (non-fatal)", exc_info=True)

        return tools


async def invalidate_marketplace_cache() -> None:
    """Clear the marketplace tools cache.  Called after publish/unpublish mutations."""
    global _marketplace_cache
    _marketplace_cache = None
    try:
        from marketplace import _invalidate_all_org_tool_price_cache

        await _invalidate_all_org_tool_price_cache()
    except Exception:
        logger.debug("Org tool marketplace price cache invalidation failed", exc_info=True)

    redis = get_redis()
    if redis is not None:
        try:
            await redis.delete("teardrop:marketplace:tools")
        except Exception:
            logger.warning("Redis marketplace cache invalidation failed (non-fatal)", exc_info=True)
