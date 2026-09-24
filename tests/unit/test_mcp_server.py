# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Smoke tests for MCP tool registration."""

from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock


def test_mcp_server_import_registers_tools_without_crashing():
    # Ensure module-level registration path executes in this test process.
    sys.modules.pop("tools.mcp_server", None)
    mod = importlib.import_module("tools.mcp_server")
    assert hasattr(mod, "mcp")


async def test_refresh_mcp_tool_reputations_updates_dynamic_descriptions(monkeypatch):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()

    async def _fake_reputation():
        return {
            "platform/calculate": {
                "reputation_score": 0.9,
                "success_rate": 0.98,
                "sample_size": 150,
                "average_latency_ms": 210,
            }
        }

    monkeypatch.setattr("marketplace.reputation.get_public_reputation", _fake_reputation)

    await mod.refresh_mcp_tool_reputations(server)

    tools = await server.list_tools()
    calculate = next(tool for tool in tools if tool.name == "calculate")
    assert "Observed quality: score=0.90, success=98.0%, sample_size=150, latency=210ms." in calculate.description
    assert calculate.meta == {
        "teardrop/reputation": {
            "reputation_score": 0.9,
            "success_rate": 0.98,
            "sample_size": 150,
            "average_latency_ms": 210,
        }
    }
    assert calculate.model_dump(by_alias=True, exclude_none=True)["_meta"] == calculate.meta

    await mod.refresh_mcp_tool_reputations(server)

    tools = await server.list_tools()
    calculate = next(tool for tool in tools if tool.name == "calculate")
    assert calculate.description.count("Observed quality:") == 1


async def test_refresh_reputation_failure_keeps_tools_and_does_not_log_secrets(monkeypatch, caplog):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    initial_tools = await server.list_tools()

    async def _unavailable():
        raise RuntimeError("sensitive-marker")

    monkeypatch.setattr("marketplace.reputation.get_public_reputation", _unavailable)
    await mod.refresh_mcp_tool_reputations(server)

    assert await server.list_tools() == initial_tools
    assert "sensitive-marker" not in caplog.text


async def test_refresh_reputation_replaces_stale_metrics(monkeypatch):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    metrics = {"platform/calculate": {"reputation_score": 0.9, "sample_size": 5}}

    async def _reputation():
        return metrics

    monkeypatch.setattr("marketplace.reputation.get_public_reputation", _reputation)
    await mod.refresh_mcp_tool_reputations(server)
    metrics = {"platform/calculate": {"reputation_score": 0.6, "sample_size": 6}}
    await mod.refresh_mcp_tool_reputations(server)

    calculate = next(tool for tool in await server.list_tools() if tool.name == "calculate")
    assert calculate.meta == {"teardrop/reputation": {"reputation_score": 0.6, "sample_size": 6}}
    assert calculate.description.count("Observed quality:") == 1
    assert "score=0.60" in calculate.description

    metrics = {}
    await mod.refresh_mcp_tool_reputations(server)
    calculate = next(tool for tool in await server.list_tools() if tool.name == "calculate")
    assert calculate.meta is None
    assert "Observed quality:" not in calculate.description


async def test_app_lifespan_cancels_mcp_reputation_refresh(monkeypatch):
    app_module = importlib.import_module("teardrop.app")
    refresh = AsyncMock()
    scheduled = asyncio.Event()
    cancelled = asyncio.Event()

    @asynccontextmanager
    async def _no_db_lifespan(_app=None):
        yield

    async def _periodic(_name, operation, interval):
        assert interval == 300
        await operation()
        scheduled.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(app_module, "lifespan", _no_db_lifespan)
    monkeypatch.setattr(app_module._mcp_server.session_manager, "run", _no_db_lifespan)
    monkeypatch.setattr(app_module, "refresh_mcp_tool_reputations", refresh)
    monkeypatch.setattr(app_module, "_run_periodic", _periodic)

    async with app_module._app_lifespan(app_module.app):
        await scheduled.wait()

    assert cancelled.is_set()
    assert refresh.await_count == 2
