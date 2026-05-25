"""Transport integration-style tests for MCP client tool discovery and invocation.

These tests start a real local FastMCP server over Streamable HTTP and validate
that mcp_client builds LangChain tools and successfully invokes them.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

import mcp_client
from mcp_client import OrgMcpServer


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def echo_server_url() -> str:
    """Start a real local Streamable HTTP MCP server and return its endpoint URL."""
    app = FastMCP(name="echo")

    @app.tool(name="echo_greeting")
    def echo_greeting(name: str) -> str:
        return f"Hello, {name}!"

    port = _find_free_port()
    asgi_app = app.streamable_http_app()

    server = uvicorn.Server(
        uvicorn.Config(
            asgi_app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    serve_task = asyncio.create_task(server.serve())

    deadline = time.monotonic() + 3.0
    while not server.started and time.monotonic() < deadline:
        if serve_task.done():
            await serve_task
            raise RuntimeError("FastMCP test server exited before startup")
        await asyncio.sleep(0.05)

    if not server.started:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(serve_task, timeout=5)
        raise RuntimeError("Timed out waiting for FastMCP test server to start")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        if not serve_task.done():
            try:
                await asyncio.wait_for(serve_task, timeout=5)
            except asyncio.TimeoutError:
                serve_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task


@pytest.fixture
async def isolated_mcp_client_state(monkeypatch):
    """Isolate module-level state so tests run independently and DB-free."""
    monkeypatch.setattr(mcp_client, "_tools_cache", {})
    monkeypatch.setattr(mcp_client, "_sessions", {})
    monkeypatch.setattr(mcp_client, "_server_caches", {})
    monkeypatch.setattr(mcp_client, "_record_event", AsyncMock())

    def make_server(url: str, *, name: str = "echo") -> OrgMcpServer:
        now = datetime.now(timezone.utc)
        return OrgMcpServer(
            id=f"server-{name}",
            org_id="org-1",
            name=name,
            url=url,
            auth_type="none",
            has_auth=False,
            auth_header_name=None,
            is_active=True,
            timeout_seconds=5,
            created_at=now,
            updated_at=now,
        )

    yield make_server

    await mcp_client._close_all_sessions()


@pytest.mark.anyio
async def test_build_mcp_langchain_tools_discovers_server_tool(echo_server_url, isolated_mcp_client_state, monkeypatch):
    server = isolated_mcp_client_state(echo_server_url)
    monkeypatch.setattr(mcp_client, "_get_servers_cached", AsyncMock(return_value=[server]))

    tools, by_name = await mcp_client.build_mcp_langchain_tools("org-1")

    assert len(tools) == 1
    assert tools[0].name == "echo__echo_greeting"
    assert "echo__echo_greeting" in by_name
    assert by_name["echo__echo_greeting"] is tools[0]


@pytest.mark.anyio
async def test_build_mcp_langchain_tools_ainvoke_calls_remote_tool(echo_server_url, isolated_mcp_client_state, monkeypatch):
    server = isolated_mcp_client_state(echo_server_url)
    monkeypatch.setattr(mcp_client, "_get_servers_cached", AsyncMock(return_value=[server]))

    _, by_name = await mcp_client.build_mcp_langchain_tools("org-1")
    result = await by_name["echo__echo_greeting"].ainvoke({"name": "World"})

    assert result == {"result": "Hello, World!"}
