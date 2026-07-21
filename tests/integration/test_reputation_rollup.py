"""Postgres integration tests for marketplace reputation aggregation."""

from __future__ import annotations

import json

import asyncpg
import pytest

import marketplace as marketplace_module
from marketplace import reputation_rollup_once
from migrations.runner import apply_pending


@pytest.fixture
async def reputation_db_pool(docker_postgres: str):
    """Apply the full schema and bind the marketplace worker to an isolated pool."""
    pool = await asyncpg.create_pool(docker_postgres, min_size=1, max_size=5)
    await apply_pending(pool)
    marketplace_module._pool = pool

    yield pool

    await pool.execute(
        "TRUNCATE TABLE run_decisions, tool_call_events, marketplace_tool_call_stats, org_tools, orgs RESTART IDENTITY CASCADE"
    )
    marketplace_module._pool = None
    await pool.close()


@pytest.mark.anyio
async def test_rollup_uses_canonical_owner_without_existing_stats_row(reputation_db_pool):
    """Self traffic and non-catalog events cannot create public reputation rows."""
    pool = reputation_db_pool
    await pool.execute(
        """
        INSERT INTO orgs (id, name, slug, created_at)
        VALUES
            ('author-org', 'Author Org', 'author', NOW()),
            ('caller-org', 'Caller Org', 'caller', NOW())
        """
    )
    await pool.execute(
        """
        INSERT INTO org_tools
            (id, org_id, name, description, input_schema, webhook_url,
             webhook_method, is_active, publish_as_mcp, created_at, updated_at)
        VALUES
            ('weather-tool', 'author-org', 'weather', '', '{}'::JSONB, 'https://example.com/weather',
             'GET', TRUE, TRUE, NOW(), NOW())
        """
    )
    await pool.execute(
        """
        INSERT INTO tool_call_events (id, run_id, org_id, tool_name, success, elapsed_ms)
        VALUES
            ('author-call', 'author-run', 'author-org', 'author/weather', TRUE, 100),
            ('caller-call', 'caller-run', 'caller-org', 'author/weather', FALSE, 200),
            ('internal-call', 'caller-run', 'caller-org', 'calculate', TRUE, 10)
        """
    )

    assert await pool.fetchval("SELECT COUNT(*) FROM marketplace_tool_call_stats") == 0

    assert await reputation_rollup_once() == 1

    row = await pool.fetchrow(
        """
        SELECT total_failures, total_latency_ms, total_calls
        FROM marketplace_tool_call_stats
        WHERE qualified_tool_name = 'author/weather'
        """
    )
    assert row is not None
    assert row["total_failures"] == 1
    assert row["total_latency_ms"] == 200
    assert row["total_calls"] == 0
    assert (
        await pool.fetchval("SELECT COUNT(*) FROM marketplace_tool_call_stats WHERE qualified_tool_name = 'platform/calculate'")
        == 0
    )


@pytest.mark.anyio
async def test_rollup_persists_recency_confidence_freshness_and_task_success(reputation_db_pool):
    """Recent evidence carries more weight without changing financial records."""
    pool = reputation_db_pool
    await pool.execute(
        """
        INSERT INTO orgs (id, name, slug, created_at)
        VALUES
            ('author-org', 'Author Org', 'author', NOW()),
            ('caller-org', 'Caller Org', 'caller', NOW())
        """
    )
    await pool.execute(
        """
        INSERT INTO org_tools
            (id, org_id, name, description, input_schema, webhook_url,
             webhook_method, is_active, publish_as_mcp, created_at, updated_at)
        VALUES
            ('fresh-tool', 'author-org', 'fresh_weather', '', '{}'::JSONB, 'https://example.com/fresh',
             'GET', TRUE, TRUE, NOW(), NOW()),
            ('stale-tool', 'author-org', 'stale_weather', '', '{}'::JSONB, 'https://example.com/stale',
             'GET', TRUE, TRUE, NOW(), NOW())
        """
    )
    await pool.execute(
        """
        INSERT INTO run_decisions (id, run_id, org_id, user_id, task_class, created_at)
        VALUES
            ('fresh-decision', 'fresh-run', 'caller-org', 'caller-user', 'weather', NOW()),
            ('stale-decision', 'stale-run', 'caller-org', 'caller-user', 'weather', NOW() - INTERVAL '60 days'),
            ('other-decision', 'other-run', 'caller-org', 'caller-user', 'private_prompt_fragment', NOW())
        """
    )
    await pool.execute(
        """
        INSERT INTO tool_call_events (id, run_id, org_id, tool_name, success, elapsed_ms, created_at)
        VALUES
            ('fresh-call', 'fresh-run', 'caller-org', 'author/fresh_weather', TRUE, 100, NOW()),
            ('stale-call', 'stale-run', 'caller-org', 'author/stale_weather', TRUE, 100, NOW() - INTERVAL '60 days'),
            ('other-call', 'other-run', 'caller-org', 'author/fresh_weather', TRUE, 100, NOW())
        """
    )

    assert await reputation_rollup_once() == 2

    rows = await pool.fetch(
        """
        SELECT qualified_tool_name, reputation_score, reputation_sample_size,
               reputation_confidence, reputation_freshness, reputation_task_success
        FROM marketplace_tool_call_stats
        WHERE qualified_tool_name IN ('author/fresh_weather', 'author/stale_weather')
        """
    )
    by_name = {row["qualified_tool_name"]: row for row in rows}
    fresh = by_name["author/fresh_weather"]
    stale = by_name["author/stale_weather"]

    assert fresh["reputation_sample_size"] > stale["reputation_sample_size"]
    assert fresh["reputation_confidence"] > stale["reputation_confidence"]
    assert fresh["reputation_freshness"] > stale["reputation_freshness"]
    assert fresh["reputation_score"] > stale["reputation_score"]
    task_success = fresh["reputation_task_success"]
    if isinstance(task_success, str):
        task_success = json.loads(task_success)
    assert task_success["data_retrieval"]["success_rate"] > 0
    assert task_success["other"]["success_rate"] > 0
    assert "private_prompt_fragment" not in task_success
