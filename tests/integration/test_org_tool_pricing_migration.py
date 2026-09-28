# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Postgres integration tests for migration 112 tri-state org tool pricing."""

from __future__ import annotations

from pathlib import Path

import pytest

import marketplace as marketplace_module
from billing import resolve_tool_cost
from marketplace import _invalidate_all_org_tool_price_cache, get_org_tool_price_by_qualified_name
from migrations.runner import apply_pending
from shared.db_pool import create_pool

_MIGRATION_SQL = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "112_org_tool_price_default_null.sql"
).read_text(encoding="utf-8")


@pytest.fixture
async def pricing_db_pool(docker_postgres: str):
    pool = await create_pool(docker_postgres, min_size=1, max_size=5, name="integration-org-tool-pricing")
    await apply_pending(pool)
    await pool.execute("TRUNCATE TABLE org_tools, orgs RESTART IDENTITY CASCADE")
    marketplace_module._pool = pool
    await _invalidate_all_org_tool_price_cache()

    yield pool

    await _invalidate_all_org_tool_price_cache()
    await pool.execute("TRUNCATE TABLE org_tools, orgs RESTART IDENTITY CASCADE")
    marketplace_module._pool = None
    await pool.close()


async def _insert_tool(pool, tool_id: str, name: str, price: int | None) -> None:
    await pool.execute(
        """
        INSERT INTO org_tools
            (id, org_id, name, description, input_schema, webhook_url, webhook_method,
             is_active, publish_as_mcp, base_price_usdc, created_at, updated_at)
        VALUES ($1, 'author-org', $2, '', '{}'::JSONB, 'https://example.com/hook', 'GET',
                TRUE, TRUE, $3, NOW(), NOW())
        """,
        tool_id,
        name,
        price,
    )


async def _price(pool, tool_id: str) -> int | None:
    return await pool.fetchval("SELECT base_price_usdc FROM org_tools WHERE id = $1", tool_id)


@pytest.mark.anyio
async def test_backfill_maps_legacy_zero_to_default_and_rerun_preserves_explicit_zero(pricing_db_pool):
    pool = pricing_db_pool
    await pool.execute("INSERT INTO orgs (id, name, slug, created_at) VALUES ('author-org', 'Author', 'author', NOW())")
    await _insert_tool(pool, "legacy-zero", "legacy_zero", 0)
    await _insert_tool(pool, "priced", "priced", 2500)
    # Recreate the pre-112 column shape so the backfill branch runs.
    await pool.execute("ALTER TABLE org_tools ALTER COLUMN base_price_usdc SET DEFAULT 0")
    await pool.execute("ALTER TABLE org_tools ALTER COLUMN base_price_usdc SET NOT NULL")

    await pool.execute(_MIGRATION_SQL)

    assert await _price(pool, "legacy-zero") is None
    assert await _price(pool, "priced") == 2500
    column = await pool.fetchrow(
        """
        SELECT is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'org_tools' AND column_name = 'base_price_usdc'
        """
    )
    assert column["is_nullable"] == "YES"
    assert column["column_default"] is None

    await _insert_tool(pool, "free", "free", 0)
    await pool.execute(_MIGRATION_SQL)

    assert await _price(pool, "free") == 0
    assert await _price(pool, "legacy-zero") is None


@pytest.mark.anyio
async def test_resolver_applies_tri_state_prices_from_postgres(pricing_db_pool):
    pool = pricing_db_pool
    await pool.execute("INSERT INTO orgs (id, name, slug, created_at) VALUES ('author-org', 'Author', 'author', NOW())")
    await _insert_tool(pool, "default-tool", "default_tool", None)
    await _insert_tool(pool, "free-tool", "free_tool", 0)
    await _insert_tool(pool, "priced-tool", "priced_tool", 2500)

    assert await get_org_tool_price_by_qualified_name("author/default_tool") is None
    assert await resolve_tool_cost("author/default_tool", {}, 1000, True) == 1000
    assert await resolve_tool_cost("author/free_tool", {}, 1000, True) == 0
    assert await resolve_tool_cost("author/priced_tool", {}, 1000, True) == 2500
    assert await resolve_tool_cost("author/missing_tool", {}, 1000, True) == 1000
