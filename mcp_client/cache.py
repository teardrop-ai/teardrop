# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-org server-list caching (Redis → in-process TTL) for the MCP client."""

from __future__ import annotations

import json

from mcp_client.base import OrgMcpServer, logger
from teardrop.cache import TTLCache, get_redis
from teardrop.config import get_settings

# ─── Per-org TTL cache (server list) ──────────────────────────────────────────

_server_caches: dict[str, TTLCache[list[OrgMcpServer]]] = {}


def _load_servers(org_id: str):
    """Lazy loader for the server cache (breaks the cache↔crud import cycle)."""
    from mcp_client.crud import list_org_mcp_servers

    return list_org_mcp_servers(org_id, active_only=True)


def _get_server_cache(org_id: str) -> TTLCache[list[OrgMcpServer]]:
    if org_id not in _server_caches:
        _server_caches[org_id] = TTLCache(
            name=f"org_mcp_servers:{org_id}",
            redis_key=f"teardrop:org_mcp_servers:{org_id}",
            ttl_seconds_fn=lambda: get_settings().mcp_client_tool_cache_ttl_seconds,
            loader=lambda: _load_servers(org_id),
            serialize=lambda servers: json.dumps([s.model_dump(mode="json") for s in servers]),
            deserialize=lambda raw: [OrgMcpServer(**item) for item in json.loads(raw)],
        )
    return _server_caches[org_id]


async def _get_servers_cached(org_id: str) -> list[OrgMcpServer]:
    """Return active MCP servers for an org with TTL cache (Redis → in-process)."""
    return await _get_server_cache(org_id).get() or []


async def invalidate_mcp_cache(org_id: str) -> None:
    """Clear the server list cache and tool cache for an org."""
    from mcp_client.runtime import _tools_cache

    await _get_server_cache(org_id).invalidate()
    _tools_cache.pop(org_id, None)
    r = get_redis()
    if r is not None:
        try:
            await r.delete(f"teardrop:org_mcp_tools:{org_id}")
        except Exception:
            logger.warning("Redis MCP tools cache invalidation failed (non-fatal)", exc_info=True)
