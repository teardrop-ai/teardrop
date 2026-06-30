# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Public-safe marketplace usage aggregates.

This module records aggregate call counts for catalog/social-proof surfaces.
It is intentionally separate from ``tool_author_earnings`` because that table
is a financial ledger and does not cover platform tools.
"""

from __future__ import annotations

import logging
from typing import Literal

from marketplace._catalog_pricing import get_platform_tool_price
from marketplace.catalog import PLATFORM_SLUG, get_marketplace_tool_by_name
from marketplace.context import _get_pool

logger = logging.getLogger(__name__)

ToolType = Literal["platform", "community"]


async def record_marketplace_tool_call(
    qualified_tool_name: str,
    *,
    tool_type: ToolType,
    author_org_id: str | None = None,
    increment: int = 1,
) -> None:
    """Increment the public aggregate call count for a marketplace tool.

    This is analytics-only.  It must never block or roll back a completed paid
    tool call, so callers should invoke it after successful billing settlement.
    """
    if increment <= 0:
        return
    pool = _get_pool()
    await pool.execute(
        """
        INSERT INTO marketplace_tool_call_stats
            (qualified_tool_name, tool_type, author_org_id, total_calls,
             first_call_at, last_call_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW(), NOW(), NOW())
        ON CONFLICT (qualified_tool_name) DO UPDATE
            SET total_calls = marketplace_tool_call_stats.total_calls + EXCLUDED.total_calls,
                tool_type = EXCLUDED.tool_type,
                author_org_id = COALESCE(EXCLUDED.author_org_id, marketplace_tool_call_stats.author_org_id),
                last_call_at = EXCLUDED.last_call_at,
                updated_at = EXCLUDED.updated_at
        """,
        qualified_tool_name,
        tool_type,
        author_org_id,
        increment,
    )


async def record_marketplace_tool_usage(tool_name: str) -> bool:
    """Resolve a runtime tool name and increment its marketplace call count.

    Returns True when the name represented a marketplace catalog tool.  Bare
    names are counted only when they are active platform marketplace tools.
    """
    if not tool_name:
        return False

    if "/" in tool_name:
        org_slug, bare_name = tool_name.split("/", 1)
        if not org_slug or not bare_name:
            return False
        if org_slug == PLATFORM_SLUG:
            platform_price = await get_platform_tool_price(bare_name)
            if platform_price is None:
                return False
            await record_marketplace_tool_call(tool_name, tool_type="platform")
            return True
        tool_row = await get_marketplace_tool_by_name(bare_name, org_slug)
        if tool_row is None:
            return False
        await record_marketplace_tool_call(
            tool_name,
            tool_type="community",
            author_org_id=tool_row.get("org_id"),
        )
        return True

    platform_price = await get_platform_tool_price(tool_name)
    if platform_price is None:
        return False
    await record_marketplace_tool_call(f"{PLATFORM_SLUG}/{tool_name}", tool_type="platform")
    return True


async def record_marketplace_tool_usage_many(tool_names: list[str]) -> None:
    """Best-effort aggregate stats recording for a list of runtime tool names."""
    for tool_name in tool_names:
        try:
            await record_marketplace_tool_usage(str(tool_name))
        except Exception:
            logger.debug("Failed to record marketplace tool usage for %s", tool_name, exc_info=True)
