"""Unit tests for mcp_client module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

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

    monkeypatch.setattr(mcp_client.base, "_pool", mock_pool)
    monkeypatch.setattr(mcp_client.cache, "_server_caches", {})
    monkeypatch.setattr(mcp_client.runtime, "_tools_cache", {})
    monkeypatch.setattr(mcp_client.session, "_sessions", {})
    # Stub out cache invalidation (looked up in the crud submodule)
    monkeypatch.setattr(mcp_client.crud, "invalidate_mcp_cache", AsyncMock())
    # Stub out audit logging (looked up in the crud submodule)
    monkeypatch.setattr(mcp_client.crud, "_record_event", AsyncMock())
    return mock_pool


@pytest.mark.anyio
async def test_create_server_success(setup_mcp_client, monkeypatch):
    pool = setup_mcp_client
    pool.fetchval = AsyncMock(return_value=0)
    pool.execute = AsyncMock()

    monkeypatch.setattr("mcp_client.crud._encrypt_token", lambda v: "encrypted")

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

    monkeypatch.setattr(mcp_client.crud, "_evict_session", AsyncMock())

    from mcp_client import delete_org_mcp_server

    result = await delete_org_mcp_server("srv-1", "org-1", actor_id="user-1")
    assert result is True


@pytest.mark.anyio
async def test_delete_server_not_found(setup_mcp_client, monkeypatch):
    pool = setup_mcp_client
    pool.execute = AsyncMock(return_value="UPDATE 0")

    import mcp_client

    monkeypatch.setattr(mcp_client.crud, "_evict_session", AsyncMock())

    from mcp_client import delete_org_mcp_server

    result = await delete_org_mcp_server("nonexistent", "org-1", actor_id="user-1")
    assert result is False


# ─── Cascade: dependent marketplace listings ─────────────────────────────────


@pytest.mark.anyio
async def test_delete_server_cascades_dependent_tools(setup_mcp_client, monkeypatch):
    """Soft-deleting a server deactivates all its published marketplace tools."""
    pool = setup_mcp_client
    pool.execute = AsyncMock(return_value="UPDATE 1")
    pool.fetchrow = AsyncMock(return_value={"name": "my_server"})
    pool.fetch = AsyncMock(return_value=[{"id": "tool-a"}, {"id": "tool-b"}])

    import mcp_client

    monkeypatch.setattr(mcp_client.crud, "_evict_session", AsyncMock())
    deact_mock = AsyncMock()
    monkeypatch.setattr("marketplace.auto_deactivate_tool_for_health", deact_mock)

    from mcp_client import delete_org_mcp_server

    result = await delete_org_mcp_server("srv-1", "org-1", actor_id="user-1")
    assert result is True
    assert deact_mock.await_count == 2
    deact_mock.assert_any_await(
        "tool-a",
        event_actor_id="system:mcp_server_cascade",
        event_reason="mcp_server_disabled_or_removed",
        notification_reason="automatic — backing MCP server was disabled or removed",
        capture_sentry=False,
    )
    deact_mock.assert_any_await(
        "tool-b",
        event_actor_id="system:mcp_server_cascade",
        event_reason="mcp_server_disabled_or_removed",
        notification_reason="automatic — backing MCP server was disabled or removed",
        capture_sentry=False,
    )


@pytest.mark.anyio
async def test_delete_server_no_dependent_tools(setup_mcp_client, monkeypatch):
    """When no dependent tools exist, cascade is a no-op (no deactivation calls)."""
    pool = setup_mcp_client
    pool.execute = AsyncMock(return_value="UPDATE 1")
    pool.fetchrow = AsyncMock(return_value={"name": "my_server"})
    pool.fetch = AsyncMock(return_value=[])

    import mcp_client

    monkeypatch.setattr(mcp_client.crud, "_evict_session", AsyncMock())
    deact_mock = AsyncMock()
    monkeypatch.setattr("marketplace.auto_deactivate_tool_for_health", deact_mock)

    from mcp_client import delete_org_mcp_server

    result = await delete_org_mcp_server("srv-1", "org-1", actor_id="user-1")
    assert result is True
    deact_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_update_server_disable_cascades(setup_mcp_client, monkeypatch):
    """Setting is_active=False on an active server cascades to its tools."""
    pool = setup_mcp_client
    # Current row is active; update returns the deactivated row.
    pool.fetchrow = AsyncMock(
        side_effect=[
            _make_db_row(is_active=True),  # initial SELECT
            _make_db_row(is_active=False),  # RETURNING *
        ]
    )
    pool.fetch = AsyncMock(return_value=[{"id": "tool-a"}])

    import mcp_client

    monkeypatch.setattr(mcp_client.crud, "_evict_session", AsyncMock())
    deact_mock = AsyncMock()
    monkeypatch.setattr("marketplace.auto_deactivate_tool_for_health", deact_mock)

    from mcp_client import update_org_mcp_server

    result = await update_org_mcp_server("srv-1", "org-1", actor_id="user-1", is_active=False)
    assert result is not None
    deact_mock.assert_awaited_once_with(
        "tool-a",
        event_actor_id="system:mcp_server_cascade",
        event_reason="mcp_server_disabled_or_removed",
        notification_reason="automatic — backing MCP server was disabled or removed",
        capture_sentry=False,
    )


@pytest.mark.anyio
async def test_update_server_enable_does_not_cascade(setup_mcp_client, monkeypatch):
    """Re-enabling a server must NOT auto-reactivate tools (manual re-enable only)."""
    pool = setup_mcp_client
    pool.fetchrow = AsyncMock(
        side_effect=[
            _make_db_row(is_active=False),  # initial SELECT (currently inactive)
            _make_db_row(is_active=True),  # RETURNING *
        ]
    )

    import mcp_client

    monkeypatch.setattr(mcp_client.crud, "_evict_session", AsyncMock())
    deact_mock = AsyncMock()
    monkeypatch.setattr("marketplace.auto_deactivate_tool_for_health", deact_mock)

    from mcp_client import update_org_mcp_server

    result = await update_org_mcp_server("srv-1", "org-1", actor_id="user-1", is_active=True)
    assert result is not None
    deact_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_update_server_url_change_does_not_cascade(setup_mcp_client, monkeypatch):
    """A URL-only update must not trigger cascade (only is_active False→True transition does)."""
    pool = setup_mcp_client
    pool.fetchrow = AsyncMock(
        side_effect=[
            _make_db_row(is_active=True),
            _make_db_row(is_active=True, url="https://new.example.com/sse"),
        ]
    )

    import mcp_client

    monkeypatch.setattr(mcp_client.crud, "_evict_session", AsyncMock())
    monkeypatch.setattr("tools.definitions.http_fetch.async_validate_url", AsyncMock(return_value=None))
    deact_mock = AsyncMock()
    monkeypatch.setattr("marketplace.auto_deactivate_tool_for_health", deact_mock)

    from mcp_client import update_org_mcp_server

    result = await update_org_mcp_server("srv-1", "org-1", actor_id="user-1", url="https://new.example.com/sse")
    assert result is not None
    deact_mock.assert_not_awaited()


# ─── build_mcp_langchain_tools (empty case) ──────────────────────────────────


@pytest.mark.anyio
async def test_build_mcp_langchain_tools_no_servers(setup_mcp_client, monkeypatch):
    import mcp_client

    monkeypatch.setattr(mcp_client.runtime, "_get_servers_cached", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_client.crud, "invalidate_mcp_cache", AsyncMock())

    from mcp_client import build_mcp_langchain_tools

    tools, by_name = await build_mcp_langchain_tools("org-1")
    assert tools == []
    assert by_name == {}


@pytest.mark.anyio
async def test_wrap_mcp_tool_marks_truncated_responses(monkeypatch):
    import mcp_client

    server = _make_server()

    class _Part:
        def __init__(self, text: str):
            self.text = text

    class _Result:
        def __init__(self, content):
            self.content = content

    class _Session:
        async def call_tool(self, tool_name, kwargs):
            return _Result([_Part("x" * (mcp_client._MAX_RESPONSE_BYTES + 1))])

    tool = mcp_client._wrap_mcp_tool(server, "list_files", "List files", {"type": "object"})

    monkeypatch.setattr(mcp_client.runtime, "_get_or_create_session", AsyncMock(return_value=_Session()))
    result = await tool.ainvoke({})

    assert "TRUNCATED" in result["result"]
