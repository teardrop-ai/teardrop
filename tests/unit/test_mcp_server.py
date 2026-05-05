"""Smoke tests for MCP tool registration."""

from __future__ import annotations

import importlib
import sys


def test_mcp_server_import_registers_tools_without_crashing():
    # Ensure module-level registration path executes in this test process.
    sys.modules.pop("tools.mcp_server", None)
    mod = importlib.import_module("tools.mcp_server")
    assert hasattr(mod, "mcp")
