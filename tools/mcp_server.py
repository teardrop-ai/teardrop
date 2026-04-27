# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Standalone FastMCP server exposing Teardrop tools over MCP protocol.

Run independently for tool discovery and reuse across multiple agents:
    python tools/mcp_server.py

The server listens on stdio by default (suitable for Claude Desktop / MCP clients).
Pass --transport=sse to expose via HTTP SSE instead.

Tools are auto-registered from the ToolRegistry — adding a new ToolDefinition
in tools/definitions/ will automatically expose it here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from typing import Any

import fastmcp

from tools import registry

logger = logging.getLogger(__name__)

# ─── Build FastMCP server ─────────────────────────────────────────────────────

mcp = fastmcp.FastMCP(
    name="teardrop-tools",
    version="1.0.0",
    instructions=("Teardrop MCP tool server. Provides tools auto-registered from the Teardrop tool registry."),
)


def _register_tools_with_mcp() -> None:
    """Auto-register all active tools from the registry with FastMCP."""
    for tool_def in registry.to_mcp_tool_defs():
        name = tool_def["name"]
        description = tool_def["description"]
        input_schema = tool_def["input_schema"]
        implementation = tool_def["implementation"]

        # Create a closure to capture the current tool_def values
        def _make_handler(impl: Any, schema: Any) -> Any:
            async def handler(**kwargs: Any) -> Any:
                validated = schema(**kwargs)
                return await impl(**validated.model_dump())

            # Inject an explicit typed signature from the Pydantic model so
            # FastMCP 3.x can build its JSON schema (it inspects __signature__).
            params = [
                inspect.Parameter(
                    name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=fi.annotation if fi.annotation is not None else Any,
                    default=(inspect.Parameter.empty if fi.is_required() else fi.default),
                )
                for name, fi in schema.model_fields.items()
            ]
            handler.__signature__ = inspect.Signature(params)
            handler.__annotations__ = {p.name: p.annotation for p in params if p.annotation is not inspect.Parameter.empty}
            return handler

        handler = _make_handler(implementation, input_schema)
        handler.__name__ = f"mcp_{name}"
        handler.__doc__ = description

        mcp.tool(description=description)(handler)
        logger.debug("MCP: registered tool %s", name)


_register_tools_with_mcp()

# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for teardrop-mcp command."""
    transport = "stdio"
    for arg in sys.argv[1:]:
        if arg.startswith("--transport="):
            transport = arg.split("=", 1)[1]

    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Teardrop MCP server (transport=%s)", transport)
    asyncio.run(mcp.run_async(transport=transport))


if __name__ == "__main__":
    main()
