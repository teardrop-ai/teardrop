# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace earnings ledger helpers."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import sentry_sdk

from marketplace.catalog import get_author_config
from marketplace.context import _get_pool
from marketplace.models import AuthorEarning, AuthorEarningByTool
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


async def record_tool_call_earnings(
    author_org_id: str,
    tool_name: str,
    caller_org_id: str,
    total_cost_usdc: int,
) -> None:
    """Record a per-call earnings entry. Fire-and-forget safe."""
    try:
        pool = _get_pool()

        config = await get_author_config(author_org_id)
        if config is None:
            logger.warning(
                "No author config for org_id=%s; earnings not recorded for tool=%s",
                author_org_id,
                tool_name,
            )
            return

        bps = get_settings().marketplace_default_revenue_share_bps
        author_share = (total_cost_usdc * bps) // 10_000
        platform_share = total_cost_usdc - author_share

        await pool.execute(
            """
            INSERT INTO tool_author_earnings
                (id, org_id, tool_name, caller_org_id, amount_usdc,
                 author_share_usdc, platform_share_usdc, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
            """,
            str(uuid.uuid4()),
            author_org_id,
            tool_name,
            caller_org_id,
            total_cost_usdc,
            author_share,
            platform_share,
        )
    except Exception as exc:
        logger.warning("Failed to record tool earnings", exc_info=True)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("author_org_id", str(author_org_id))
            scope.set_tag("caller_org_id", str(caller_org_id))
            scope.set_tag("tool_name", str(tool_name))
            sentry_sdk.capture_exception(exc)


async def get_author_balance(org_id: str) -> int:
    """Return the total pending (unsettled) author earnings in atomic USDC."""
    pool = _get_pool()
    result = await pool.fetchval(
        "SELECT COALESCE(SUM(author_share_usdc), 0) FROM tool_author_earnings WHERE org_id = $1 AND status = 'pending'",
        org_id,
    )
    return int(result)


async def get_author_earnings_history(
    org_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
    tool_name: str | None = None,
) -> tuple[list[AuthorEarning], str | None]:
    """Return earnings history for an org, cursor-paginated by created_at DESC."""
    pool = _get_pool()
    base_where = "WHERE org_id = $1"
    params: list = [org_id, limit]

    if tool_name is not None:
        base_where += " AND tool_name = $3"
        params.append(tool_name)

    if cursor is not None:
        cursor_clause = f"AND created_at < ${len(params) + 1}"
        params.append(cursor)
    else:
        cursor_clause = ""

    rows = await pool.fetch(
        f"""
        SELECT id, org_id, tool_name, caller_org_id, amount_usdc,
               author_share_usdc, platform_share_usdc, status, created_at
        FROM tool_author_earnings
        {base_where} {cursor_clause}
        ORDER BY created_at DESC
        LIMIT $2
        """,
        *params,
    )
    earnings = [AuthorEarning(**dict(r)) for r in rows]
    next_cursor = earnings[-1].created_at.isoformat() if len(earnings) == limit else None
    return earnings, next_cursor


async def get_author_earnings_by_tool(org_id: str) -> list[AuthorEarningByTool]:
    """Return per-tool earnings aggregates for an org."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT tool_name,
               COUNT(*) AS total_calls,
               COALESCE(SUM(amount_usdc), 0) AS total_amount_usdc,
               COALESCE(SUM(author_share_usdc), 0) AS total_author_share_usdc,
               COALESCE(SUM(author_share_usdc) FILTER (WHERE status = 'pending'), 0)
                   AS pending_author_share_usdc,
               COALESCE(SUM(author_share_usdc) FILTER (WHERE status = 'settled'), 0)
                   AS settled_author_share_usdc,
               COALESCE(SUM(platform_share_usdc), 0) AS total_platform_share_usdc
        FROM tool_author_earnings
        WHERE org_id = $1
        GROUP BY tool_name
        ORDER BY total_author_share_usdc DESC, tool_name ASC
        """,
        org_id,
    )
    return [AuthorEarningByTool(**dict(r)) for r in rows]
