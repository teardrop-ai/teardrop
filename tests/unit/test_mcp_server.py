# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Smoke tests for MCP tool registration."""

from __future__ import annotations

import importlib
import sys


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

    await mod.refresh_mcp_tool_reputations(server)

    tools = await server.list_tools()
    calculate = next(tool for tool in tools if tool.name == "calculate")
    assert calculate.description.count("Observed quality:") == 1
