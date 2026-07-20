"""Postgres integration tests for MCP discovery schema tracking."""

from __future__ import annotations

import pytest

import mcp_client as mcp_module
import teardrop.users as user_module
from mcp_client import create_org_mcp_server, get_org_mcp_server
from mcp_client.crud import record_mcp_server_schema_hash
from migrations.runner import apply_pending
from teardrop.users import create_org


@pytest.fixture
async def schema_hash_db_pool(db_pool):
    """Bind MCP persistence to a schema-complete integration pool."""
    await apply_pending(db_pool)
    mcp_module.base._pool = db_pool
    user_module.base._pool = db_pool
    mcp_module.cache._server_caches.clear()
    mcp_module.runtime._tools_cache.clear()

    yield db_pool

    await db_pool.execute("TRUNCATE TABLE org_mcp_server_events, org_mcp_servers, orgs RESTART IDENTITY CASCADE")
    mcp_module.base._pool = None
    user_module.base._pool = None


@pytest.mark.anyio
async def test_schema_hash_compare_and_swap_preserves_newer_discovery(schema_hash_db_pool, monkeypatch):
    """A late discovery based on an old hash cannot regress stored inventory."""
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)
    org = await create_org("MCP Schema CAS Test Org")
    server = await create_org_mcp_server(
        org.id,
        name="schema_guard",
        url="https://schema.example.com",
        actor_id="user-1",
    )

    baseline_writer = await get_org_mcp_server(server.id, org.id)
    assert baseline_writer is not None
    assert await record_mcp_server_schema_hash(baseline_writer, "a" * 64) == (True, False)

    stale_writer = await get_org_mcp_server(server.id, org.id)
    newer_writer = await get_org_mcp_server(server.id, org.id)
    assert stale_writer is not None
    assert newer_writer is not None

    assert await record_mcp_server_schema_hash(newer_writer, "b" * 64) == (True, True)
    assert await record_mcp_server_schema_hash(stale_writer, "c" * 64) == (False, False)

    stored = await get_org_mcp_server(server.id, org.id)
    assert stored is not None
    assert stored.schema_hash == "b" * 64
