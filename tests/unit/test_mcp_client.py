"""Unit tests for mcp_client module."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_client import OrgMcpServer

# ─── Fixtures ─────────────────────────────────────────────────────────────────

_NOW = datetime.now(timezone.utc)


def _make_server(**overrides: object) -> OrgMcpServer:
    defaults = {
        "id": "srv-1",
        "org_id": "org-1",
        "name": "my_server",
        "url": "https://mcp.example.com/sse",
        "auth_type": "none",
        "has_auth": False,
        "auth_header_name": None,
        "is_active": True,
        "timeout_seconds": 15,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return OrgMcpServer(**defaults)


def _make_db_row(**overrides: object) -> dict:
    defaults = {
        "id": "srv-1",
        "org_id": "org-1",
        "name": "my_server",
        "url": "https://mcp.example.com/sse",
        "auth_type": "none",
        "auth_token_enc": None,
        "auth_header_name": None,
        "is_active": True,
        "timeout_seconds": 15,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return defaults


# ─── Model tests ──────────────────────────────────────────────────────────────


def test_org_mcp_server_model():
    srv = _make_server()
    assert srv.id == "srv-1"
    assert srv.has_auth is False
    assert srv.auth_type == "none"


def test_org_mcp_server_with_auth():
    srv = _make_server(auth_type="bearer", has_auth=True)
    assert srv.has_auth is True
    assert srv.auth_type == "bearer"


# ─── _row_to_model tests ─────────────────────────────────────────────────────


def test_row_to_model():
    from mcp_client import _row_to_model

    row = _make_db_row()
    srv = _row_to_model(row)
    assert srv.id == "srv-1"
    assert srv.has_auth is False


def test_row_to_model_with_auth():
    from mcp_client import _row_to_model

    row = _make_db_row(auth_token_enc="encrypted_value")
    srv = _row_to_model(row)
    assert srv.has_auth is True


# ─── CRUD tests (mocked DB pool) ─────────────────────────────────────────────


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    return pool


@pytest.fixture
def setup_mcp_client(mock_pool, test_settings, monkeypatch):
    """Inject a mock pool and stub out side-effects."""
    import mcp_client

    monkeypatch.setattr(mcp_client, "_pool", mock_pool)
    monkeypatch.setattr(mcp_client, "_servers_cache", {})
    monkeypatch.setattr(mcp_client, "_tools_cache", {})
    monkeypatch.setattr(mcp_client, "_sessions", {})
    # Stub out cache invalidation
    monkeypatch.setattr(mcp_client, "invalidate_mcp_cache", AsyncMock())
    # Stub out audit logging
    monkeypatch.setattr(mcp_client, "_record_event", AsyncMock())
    return mock_pool


@pytest.mark.anyio
async def test_create_server_success(setup_mcp_client, monkeypatch):
    pool = setup_mcp_client
    pool.fetchval = AsyncMock(return_value=0)
    pool.execute = AsyncMock()

    monkeypatch.setattr("mcp_client._encrypt_token", lambda v: "encrypted")

    from mcp_client import create_org_mcp_server

    # Stub validate_url
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    srv = await create_org_mcp_server(
        "org-1",
        name="test_server",
        url="https://mcp.example.com/sse",
        auth_type="bearer",
        auth_token="secret-token",
        actor_id="user-1",
    )
    assert srv.name == "test_server"
    assert srv.has_auth is True
    assert srv.auth_type == "bearer"
    pool.execute.assert_called_once()


@pytest.mark.anyio
async def test_create_server_quota_exceeded(setup_mcp_client, monkeypatch):
    pool = setup_mcp_client
    pool.fetchval = AsyncMock(return_value=5)

    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: None)

    from mcp_client import create_org_mcp_server

    with pytest.raises(ValueError, match="limit reached"):
        await create_org_mcp_server(
            "org-1",
            name="overflow",
            url="https://mcp.example.com/sse",
            actor_id="user-1",
        )


@pytest.mark.anyio
async def test_create_server_ssrf_blocked(setup_mcp_client, monkeypatch):
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda u: "Blocked IP")

    from mcp_client import create_org_mcp_server

    with pytest.raises(ValueError, match="URL blocked"):
        await create_org_mcp_server(
            "org-1",
            name="evil",
            url="http://169.254.169.254/",
            actor_id="user-1",
        )


@pytest.mark.anyio
async def test_list_servers(setup_mcp_client):
    pool = setup_mcp_client
    pool.fetch = AsyncMock(return_value=[_make_db_row()])

    from mcp_client import list_org_mcp_servers

    servers = await list_org_mcp_servers("org-1")
    assert len(servers) == 1
    assert servers[0].name == "my_server"


@pytest.mark.anyio
async def test_get_server_not_found(setup_mcp_client):
    pool = setup_mcp_client
    pool.fetchrow = AsyncMock(return_value=None)

    from mcp_client import get_org_mcp_server

    result = await get_org_mcp_server("nonexistent", "org-1")
    assert result is None


@pytest.mark.anyio
async def test_get_server_found(setup_mcp_client):
    pool = setup_mcp_client
    pool.fetchrow = AsyncMock(return_value=_make_db_row())

    from mcp_client import get_org_mcp_server

    result = await get_org_mcp_server("srv-1", "org-1")
    assert result is not None
    assert result.id == "srv-1"


@pytest.mark.anyio
async def test_delete_server_success(setup_mcp_client, monkeypatch):
    pool = setup_mcp_client
    pool.execute = AsyncMock(return_value="UPDATE 1")
    pool.fetchrow = AsyncMock(return_value={"name": "my_server"})

    import mcp_client

    monkeypatch.setattr(mcp_client, "_evict_session", AsyncMock())

    from mcp_client import delete_org_mcp_server

    result = await delete_org_mcp_server("srv-1", "org-1", actor_id="user-1")
    assert result is True


@pytest.mark.anyio
async def test_delete_server_not_found(setup_mcp_client, monkeypatch):
    pool = setup_mcp_client
    pool.execute = AsyncMock(return_value="UPDATE 0")

    import mcp_client

    monkeypatch.setattr(mcp_client, "_evict_session", AsyncMock())

    from mcp_client import delete_org_mcp_server

    result = await delete_org_mcp_server("nonexistent", "org-1", actor_id="user-1")
    assert result is False


# ─── build_mcp_langchain_tools (empty case) ──────────────────────────────────


@pytest.mark.anyio
async def test_build_mcp_langchain_tools_no_servers(setup_mcp_client, monkeypatch):
    import mcp_client

    monkeypatch.setattr(mcp_client, "_get_servers_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_client, "invalidate_mcp_cache", AsyncMock())

    from mcp_client import build_mcp_langchain_tools

    tools, by_name = await build_mcp_langchain_tools("org-1")
    assert tools == []
    assert by_name == {}
