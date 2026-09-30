# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Smoke tests for MCP tool registration."""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_PRICE_ZERO = {"teardrop/price": {"cost_usdc": 0, "unit": "call"}}


@pytest.fixture(autouse=True)
def _flat_pricing(monkeypatch):
    monkeypatch.setattr("billing.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("billing.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("marketplace.get_platform_tool_price", AsyncMock(return_value=None))


def _community_tool(name="acme/weather", cost=5_000, schema=None):
    return SimpleNamespace(
        qualified_name=name,
        tool_type="community",
        marketplace_description="Weather lookup",
        input_schema=schema or {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        output_schema=None,
        cost_usdc=cost,
    )


def _marketplace_on(monkeypatch, catalog):
    monkeypatch.setattr("marketplace.reputation.get_public_reputation", AsyncMock(return_value={}))
    monkeypatch.setattr("tools.mcp_server.get_settings", lambda: SimpleNamespace(marketplace_enabled=True))
    catalog_mock = AsyncMock(return_value=catalog)
    monkeypatch.setattr("marketplace.get_marketplace_catalog", catalog_mock)
    return catalog_mock


async def _names(server):
    return {tool.name for tool in await server.list_tools()}


def test_mcp_server_import_registers_tools_without_crashing():
    # Ensure module-level registration path executes in this test process.
    sys.modules.pop("tools.mcp_server", None)
    mod = importlib.import_module("tools.mcp_server")
    assert hasattr(mod, "mcp")


async def test_refresh_mcp_tools_updates_dynamic_descriptions(monkeypatch):
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

    await mod.refresh_mcp_tools(server)

    tools = await server.list_tools()
    calculate = next(tool for tool in tools if tool.name == "calculate")
    assert "Observed quality: score=0.90, success=98.0%, sample_size=150, latency=210ms." in calculate.description
    assert calculate.meta == {
        "teardrop/reputation": {
            "reputation_score": 0.9,
            "success_rate": 0.98,
            "sample_size": 150,
            "average_latency_ms": 210,
        },
        **_PRICE_ZERO,
    }
    assert calculate.model_dump(by_alias=True, exclude_none=True)["_meta"] == calculate.meta

    await mod.refresh_mcp_tools(server)

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
    await mod.refresh_mcp_tools(server)

    assert await server.list_tools() == initial_tools
    assert "sensitive-marker" not in caplog.text


async def test_refresh_pricing_failure_keeps_reputation_and_does_not_log_secrets(monkeypatch, caplog):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    monkeypatch.setattr(
        "marketplace.reputation.get_public_reputation",
        AsyncMock(return_value={"platform/calculate": {"reputation_score": 0.9, "sample_size": 5}}),
    )
    monkeypatch.setattr("billing.get_current_pricing", AsyncMock(side_effect=RuntimeError("sensitive-marker")))

    await mod.refresh_mcp_tools(server)

    calculate = next(tool for tool in await server.list_tools() if tool.name == "calculate")
    assert calculate.meta == {"teardrop/reputation": {"reputation_score": 0.9, "sample_size": 5}}
    assert "sensitive-marker" not in caplog.text


async def test_refresh_reputation_replaces_stale_metrics(monkeypatch):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    metrics = {"platform/calculate": {"reputation_score": 0.9, "sample_size": 5}}

    async def _reputation():
        return metrics

    monkeypatch.setattr("marketplace.reputation.get_public_reputation", _reputation)
    await mod.refresh_mcp_tools(server)
    metrics = {"platform/calculate": {"reputation_score": 0.6, "sample_size": 6}}
    await mod.refresh_mcp_tools(server)

    calculate = next(tool for tool in await server.list_tools() if tool.name == "calculate")
    assert calculate.meta == {"teardrop/reputation": {"reputation_score": 0.6, "sample_size": 6}, **_PRICE_ZERO}
    assert calculate.description.count("Observed quality:") == 1
    assert "score=0.60" in calculate.description

    metrics = {}
    await mod.refresh_mcp_tools(server)
    calculate = next(tool for tool in await server.list_tools() if tool.name == "calculate")
    assert calculate.meta == _PRICE_ZERO
    assert "Observed quality:" not in calculate.description


async def test_refresh_registers_priced_community_tools_and_skips_platform_rows(monkeypatch):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    platform_row = SimpleNamespace(
        qualified_name="platform/calculate",
        tool_type="platform",
        marketplace_description="",
        input_schema={},
        cost_usdc=0,
    )
    catalog_mock = _marketplace_on(monkeypatch, [_community_tool(), platform_row])

    await mod.refresh_mcp_tools(server)

    catalog_mock.assert_awaited_once_with({}, 0, limit=200)
    weather = next(tool for tool in await server.list_tools() if tool.name == "acme/weather")
    assert weather.meta == {"teardrop/price": {"cost_usdc": 5_000, "unit": "call"}}
    assert weather.input_schema["required"] == ["city"]
    assert "platform/calculate" not in await _names(server)


async def test_refresh_snapshot_matches_tools_list_including_pricing_failure(monkeypatch):
    from tools.capabilities import get_capability_manifest

    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    catalog_mock = _marketplace_on(monkeypatch, [_community_tool()])
    await mod.refresh_mcp_tools(server)

    by_name = {capability.name: capability for capability in get_capability_manifest()}
    assert by_name["acme/weather"].cost_usdc == 5_000
    assert by_name["calculate"].cost_usdc == 0
    assert await _names(server) == set(by_name)

    catalog_mock.side_effect = RuntimeError("db down")
    await mod.refresh_mcp_tools(server)

    by_name = {capability.name: capability for capability in get_capability_manifest()}
    assert await _names(server) == set(by_name)
    assert by_name["calculate"].cost_usdc is None
    assert by_name["acme/weather"].cost_usdc == 5_000


async def test_manifest_price_matches_gateway_x402_charge(monkeypatch):
    from teardrop.mcp_gateway import MCPGatewayMiddleware
    from tools.capabilities import get_capability_manifest

    monkeypatch.setattr("billing.get_tool_pricing_overrides", AsyncMock(return_value={"web_search": 15_000}))
    monkeypatch.setattr("billing.get_current_pricing", AsyncMock(return_value=SimpleNamespace(tool_call_cost=1_000)))
    monkeypatch.setattr("marketplace.reputation.get_public_reputation", AsyncMock(return_value={}))
    mod = importlib.import_module("tools.mcp_server")
    await mod.refresh_mcp_tools(mod.create_mcp_server())

    by_name = {capability.name: capability for capability in get_capability_manifest()}
    for name in ("web_search", "get_token_price"):
        assert by_name[name].cost_usdc == await MCPGatewayMiddleware._resolve_tool_cost(name)


async def test_refresh_removes_delisted_and_keeps_last_set_on_catalog_failure(monkeypatch):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    catalog_mock = _marketplace_on(monkeypatch, [_community_tool(), _community_tool("acme/news")])
    await mod.refresh_mcp_tools(server)

    catalog_mock.return_value = [_community_tool()]
    await mod.refresh_mcp_tools(server)
    assert {"acme/weather"} == {name for name in await _names(server) if "/" in name}

    catalog_mock.side_effect = RuntimeError("db down")
    await mod.refresh_mcp_tools(server)
    assert "acme/weather" in await _names(server)


async def test_refresh_unregisters_community_tools_when_marketplace_disabled(monkeypatch):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    catalog_mock = _marketplace_on(monkeypatch, [_community_tool()])
    await mod.refresh_mcp_tools(server)

    monkeypatch.setattr("tools.mcp_server.get_settings", lambda: SimpleNamespace(marketplace_enabled=False))
    await mod.refresh_mcp_tools(server)

    assert not any("/" in name for name in await _names(server))
    catalog_mock.assert_awaited_once()


async def test_refresh_skips_unsupported_community_schema(monkeypatch, caplog):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    bad = _community_tool("acme/bad", schema={"type": "object", "properties": {"from": {"type": "string"}}})
    _marketplace_on(monkeypatch, [bad, _community_tool()])

    with caplog.at_level(logging.WARNING, logger="tools.mcp_server"):
        await mod.refresh_mcp_tools(server)

    names = await _names(server)
    assert "acme/weather" in names
    assert "acme/bad" not in names
    assert "skipped community tool acme/bad" in caplog.text


async def test_community_handler_executes_live_row_with_supplied_arguments(monkeypatch):
    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    _marketplace_on(monkeypatch, [_community_tool()])
    await mod.refresh_mcp_tools(server)
    row = {"id": "t1", "org_id": "author-org", "name": "weather"}
    lookup = AsyncMock(return_value=row)
    execute = AsyncMock(return_value={"temp_c": 20})
    monkeypatch.setattr("marketplace.get_marketplace_tool_by_name", lookup)
    monkeypatch.setattr("marketplace.execution.execute_marketplace_tool", execute)

    result = await server.call_tool("acme/weather", {"city": "Paris"})

    assert result.is_error is False
    lookup.assert_awaited_once_with("weather", "acme")
    execute.assert_awaited_once_with(row, {"city": "Paris"})


@pytest.mark.parametrize(
    ("row", "outcome"),
    [(None, {"temp_c": 20}), ({"id": "t1", "org_id": "a", "name": "weather"}, {"error": "Webhook returned HTTP 500"})],
)
async def test_community_handler_surfaces_failures_as_tool_errors(monkeypatch, row, outcome):
    from mcp.server.mcpserver.exceptions import ToolError

    mod = importlib.import_module("tools.mcp_server")
    server = mod.create_mcp_server()
    _marketplace_on(monkeypatch, [_community_tool()])
    await mod.refresh_mcp_tools(server)
    monkeypatch.setattr("marketplace.get_marketplace_tool_by_name", AsyncMock(return_value=row))
    monkeypatch.setattr("marketplace.execution.execute_marketplace_tool", AsyncMock(return_value=outcome))

    with pytest.raises(ToolError):
        await server.call_tool("acme/weather", {"city": "Paris"})


async def test_created_server_never_exposes_community_tools_before_refresh():
    mod = importlib.import_module("tools.mcp_server")
    assert not any("/" in name for name in await _names(mod.create_mcp_server()))


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
    monkeypatch.setattr(app_module, "refresh_mcp_tools", refresh)
    monkeypatch.setattr(app_module, "_run_periodic", _periodic)

    async with app_module._app_lifespan(app_module.app):
        await scheduled.wait()

    assert cancelled.is_set()
    assert refresh.await_count == 2
