"""Integration tests for mcp_client CRUD against a real Postgres database.

These tests verify the full lifecycle: create, list, get, update, delete
of MCP server records, including quota enforcement, uniqueness constraints,
and audit event creation.

Skipped when Docker / DATABASE_URL is not available (same guard as conftest).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_client as mcp_module
import users as user_module
from mcp_client import (
    OrgMcpServer,
    create_org_mcp_server,
    delete_org_mcp_server,
    get_org_mcp_server,
    list_org_mcp_servers,
    update_org_mcp_server,
)
from users import create_org  # noqa: I001

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def bind_pools_and_schema(db_pool, test_settings, monkeypatch):
    """Bind module pools, apply MCP migration SQL, and set encryption key."""
    # Bind pools
    user_module._pool = db_pool
    mcp_module._pool = db_pool

    # Ensure encryption key is set for Fernet
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import config

    config.get_settings.cache_clear()
    import org_tools

    org_tools._fernet = None

    # Apply migration SQL
    migration_path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "012_org_mcp_servers.sql"
    sql = migration_path.read_text()
    await db_pool.execute(sql)

    # Clear caches
    mcp_module._servers_cache.clear()
    mcp_module._tools_cache.clear()

    yield

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE org_mcp_server_events, org_mcp_servers RESTART IDENTITY CASCADE")
    mcp_module._pool = None
    user_module._pool = None
    config.get_settings.cache_clear()


@pytest.fixture
async def test_org(db_pool):
    """Create a test organisation."""
    return await create_org("MCP Test Org")


# ─── CRUD Lifecycle ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_and_get_server(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(
        test_org.id,
        name="my_mcp",
        url="https://mcp.example.com/sse",
        actor_id="user-1",
    )
    assert isinstance(srv, OrgMcpServer)
    assert srv.name == "my_mcp"
    assert srv.is_active is True
    assert srv.has_auth is False

    # Fetch it back
    fetched = await get_org_mcp_server(srv.id, test_org.id)
    assert fetched is not None
    assert fetched.id == srv.id
    assert fetched.url == "https://mcp.example.com/sse"


@pytest.mark.anyio
async def test_create_with_bearer_auth(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(
        test_org.id,
        name="auth_mcp",
        url="https://mcp.example.com/sse",
        auth_type="bearer",
        auth_token="my-secret-token",
        actor_id="user-1",
    )
    assert srv.auth_type == "bearer"
    assert srv.has_auth is True


@pytest.mark.anyio
async def test_create_with_header_auth(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(
        test_org.id,
        name="header_mcp",
        url="https://mcp.example.com/sse",
        auth_type="header",
        auth_token="my-api-key",
        auth_header_name="X-API-Key",
        actor_id="user-1",
    )
    assert srv.auth_type == "header"
    assert srv.has_auth is True
    assert srv.auth_header_name == "X-API-Key"


@pytest.mark.anyio
async def test_list_servers(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    await create_org_mcp_server(test_org.id, name="srv_a", url="https://a.example.com", actor_id="u1")
    await create_org_mcp_server(test_org.id, name="srv_b", url="https://b.example.com", actor_id="u1")

    servers = await list_org_mcp_servers(test_org.id)
    assert len(servers) == 2
    names = {s.name for s in servers}
    assert names == {"srv_a", "srv_b"}


@pytest.mark.anyio
async def test_list_servers_active_only(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(test_org.id, name="to_delete", url="https://x.example.com", actor_id="u1")
    await delete_org_mcp_server(srv.id, test_org.id, actor_id="u1")

    active = await list_org_mcp_servers(test_org.id, active_only=True)
    assert len(active) == 0

    all_servers = await list_org_mcp_servers(test_org.id, active_only=False)
    assert len(all_servers) == 1
    assert all_servers[0].is_active is False


@pytest.mark.anyio
async def test_update_server(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(test_org.id, name="updatable", url="https://old.example.com", actor_id="u1")

    updated = await update_org_mcp_server(
        srv.id,
        test_org.id,
        actor_id="u1",
        url="https://new.example.com",
        timeout_seconds=30,
    )
    assert updated is not None
    assert updated.url == "https://new.example.com"
    assert updated.timeout_seconds == 30


@pytest.mark.anyio
async def test_update_server_not_found(test_org):
    result = await update_org_mcp_server("nonexistent", test_org.id, actor_id="u1", name="x")
    assert result is None


@pytest.mark.anyio
async def test_delete_server(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(test_org.id, name="deletable", url="https://d.example.com", actor_id="u1")

    deleted = await delete_org_mcp_server(srv.id, test_org.id, actor_id="u1")
    assert deleted is True

    fetched = await get_org_mcp_server(srv.id, test_org.id)
    assert fetched is not None
    assert fetched.is_active is False


@pytest.mark.anyio
async def test_delete_server_not_found(test_org):
    deleted = await delete_org_mcp_server("nonexistent", test_org.id, actor_id="u1")
    assert deleted is False


# ─── Uniqueness Constraint ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_duplicate_name_rejected(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    await create_org_mcp_server(test_org.id, name="unique_name", url="https://a.example.com", actor_id="u1")

    with pytest.raises(ValueError, match="already exists"):
        await create_org_mcp_server(test_org.id, name="unique_name", url="https://b.example.com", actor_id="u1")


# ─── Quota Enforcement ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_quota_enforcement(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    import config

    monkeypatch.setenv("MAX_ORG_MCP_SERVERS", "2")
    config.get_settings.cache_clear()

    await create_org_mcp_server(test_org.id, name="s1", url="https://1.example.com", actor_id="u1")
    await create_org_mcp_server(test_org.id, name="s2", url="https://2.example.com", actor_id="u1")

    with pytest.raises(ValueError, match="limit reached"):
        await create_org_mcp_server(test_org.id, name="s3", url="https://3.example.com", actor_id="u1")


# ─── Cross-tenant isolation ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_cross_tenant_isolation(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(test_org.id, name="private", url="https://x.example.com", actor_id="u1")

    # Another org cannot see this server
    fetched = await get_org_mcp_server(srv.id, "other-org-id")
    assert fetched is None


# ─── Audit trail ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_audit_events_created(test_org, db_pool, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(test_org.id, name="audited", url="https://a.example.com", actor_id="u1")

    events = await db_pool.fetch(
        "SELECT event_type FROM org_mcp_server_events WHERE server_id = $1 ORDER BY created_at",
        srv.id,
    )
    types = [e["event_type"] for e in events]
    assert "created" in types

    # Update generates an event too
    await update_org_mcp_server(srv.id, test_org.id, actor_id="u1", name="renamed")

    events = await db_pool.fetch(
        "SELECT event_type FROM org_mcp_server_events WHERE server_id = $1 ORDER BY created_at",
        srv.id,
    )
    types = [e["event_type"] for e in events]
    assert "updated" in types

    # Delete generates an event
    await delete_org_mcp_server(srv.id, test_org.id, actor_id="u1")

    events = await db_pool.fetch(
        "SELECT event_type FROM org_mcp_server_events WHERE server_id = $1 ORDER BY created_at",
        srv.id,
    )
    types = [e["event_type"] for e in events]
    assert "deleted" in types


# ─── URL validation (SSRF) ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ssrf_url_blocked(test_org, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: "Blocked IP")

    with pytest.raises(ValueError, match="URL blocked"):
        await create_org_mcp_server(
            test_org.id,
            name="evil",
            url="http://169.254.169.254/",
            actor_id="u1",
        )
