# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Cached public marketplace reputation aggregates."""

from __future__ import annotations

import json
from typing import Any

from marketplace.context import _get_pool
from teardrop.cache import TTLCache

_PUBLIC_REPUTATION_TTL_SECONDS = 300
_MIN_PUBLIC_CALLERS = 5


async def _load_public_reputation() -> dict[str, Any]:
    rows = await _get_pool().fetch(
        """
        WITH active_tools AS (
            SELECT o.slug || '/' || t.name AS qualified_tool_name
            FROM org_tools t
            JOIN orgs o ON o.id = t.org_id
            WHERE t.publish_as_mcp = TRUE
              AND t.is_active = TRUE
              AND o.slug <> 'platform'
            UNION ALL
            SELECT 'platform/' || p.tool_name AS qualified_tool_name
            FROM marketplace_platform_tools p
            WHERE p.is_active = TRUE
        )
        SELECT
            a.qualified_tool_name,
            COALESCE(s.reputation_score, 0) AS reputation_score,
            COALESCE(s.success_rate, 0) AS success_rate,
            COALESCE(s.reputation_sample_size, 0) AS reputation_sample_size,
            COALESCE(s.reputation_confidence, 0) AS reputation_confidence,
            COALESCE(s.reputation_freshness, 0) AS reputation_freshness,
            COALESCE(s.average_latency_ms, 0) AS average_latency_ms,
            COALESCE(s.unique_caller_count, 0) AS unique_caller_count,
            s.updated_at
        FROM active_tools a
        LEFT JOIN marketplace_tool_call_stats s USING (qualified_tool_name)
        ORDER BY a.qualified_tool_name
        """
    )
    reputation: dict[str, dict[str, Any]] = {}
    latest_updated_at = None
    for row in rows:
        metrics: dict[str, Any] = {
            "reputation_score": float(row["reputation_score"]),
            "success_rate": float(row["success_rate"]),
            "sample_size": float(row["reputation_sample_size"]),
            "confidence": float(row["reputation_confidence"]),
            "freshness": float(row["reputation_freshness"]),
            "average_latency_ms": float(row["average_latency_ms"]),
        }
        unique_caller_count = int(row["unique_caller_count"])
        if unique_caller_count >= _MIN_PUBLIC_CALLERS:
            metrics["unique_caller_count"] = unique_caller_count
        reputation[row["qualified_tool_name"]] = metrics
        if row["updated_at"] is not None and (latest_updated_at is None or row["updated_at"] > latest_updated_at):
            latest_updated_at = row["updated_at"]
    return {
        "generated_at": latest_updated_at.isoformat() if latest_updated_at is not None else None,
        "tools": reputation,
    }


_PUBLIC_REPUTATION_CACHE = TTLCache[dict[str, Any]](
    name="public_reputation",
    redis_key="teardrop:public_reputation",
    ttl_seconds_fn=lambda: _PUBLIC_REPUTATION_TTL_SECONDS,
    loader=_load_public_reputation,
    serialize=json.dumps,
    deserialize=json.loads,
    stale_default={"generated_at": None, "tools": {}},
)


async def get_public_reputation() -> dict[str, dict[str, Any]]:
    snapshot = await get_public_reputation_snapshot()
    return snapshot["tools"]


async def get_public_reputation_snapshot() -> dict[str, Any]:
    return await _PUBLIC_REPUTATION_CACHE.get() or {"generated_at": None, "tools": {}}


async def invalidate_public_reputation_cache() -> None:
    await _PUBLIC_REPUTATION_CACHE.invalidate()
