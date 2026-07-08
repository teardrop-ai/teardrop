# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Persisted per-org tool exclusions (durable "hide this tool" dashboard preference).

Complements the per-request ``ToolPolicy.exclude_names`` on ``POST /agent/run``
(``teardrop/agent_schemas.py``): persisted exclusions are merged with any
request-level exclusions before entering agent state, so a dashboard
preference applies to every run — including scheduled and event-triggered
runs — without the caller resending it each time.

Non-financial, advisory data only. Read failures never raise — a lookup
failure must never block ``/agent/run``; it degrades to "no persisted
exclusions" for that call, matching the ``_safe_*`` helper pattern used by
``teardrop.agent_runtime._prepare_run_context``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger(__name__)

MAX_EXCLUSIONS_PER_ORG = 50

_pool: asyncpg.Pool | None = None


async def init_tool_exclusions_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference. Called during app lifespan startup."""
    global _pool
    _pool = pool
    logger.info("Tool exclusions DB ready")


async def close_tool_exclusions_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Tool exclusions DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Tool exclusions DB not initialised — call init_tool_exclusions_db() first")
    return _pool


async def list_org_tool_exclusions(org_id: str) -> list[str]:
    """Return the persisted excluded tool names for an org. Never raises."""
    if not org_id:
        return []
    try:
        pool = _get_pool()
        rows = await pool.fetch(
            "SELECT tool_name FROM org_tool_exclusions WHERE org_id = $1 ORDER BY tool_name",
            org_id,
        )
        return [r["tool_name"] for r in rows]
    except Exception:
        logger.debug("Tool exclusion lookup failed for org_id=%s", org_id, exc_info=True)
        return []


async def add_org_tool_exclusion(org_id: str, tool_name: str) -> None:
    """Persist a tool exclusion for an org. Raises ValueError if the org's quota is exceeded."""
    pool = _get_pool()
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM org_tool_exclusions WHERE org_id = $1",
        org_id,
    )
    if count >= MAX_EXCLUSIONS_PER_ORG:
        raise ValueError(f"Tool exclusion limit reached ({MAX_EXCLUSIONS_PER_ORG})")

    await pool.execute(
        """
        INSERT INTO org_tool_exclusions (org_id, tool_name, created_at)
        VALUES ($1, $2, $3)
        ON CONFLICT (org_id, tool_name) DO NOTHING
        """,
        org_id,
        tool_name,
        datetime.now(timezone.utc),
    )


async def remove_org_tool_exclusion(org_id: str, tool_name: str) -> bool:
    """Delete a persisted exclusion. Returns True if a row was removed."""
    pool = _get_pool()
    result = await pool.execute(
        "DELETE FROM org_tool_exclusions WHERE org_id = $1 AND tool_name = $2",
        org_id,
        tool_name,
    )
    return result != "DELETE 0"
