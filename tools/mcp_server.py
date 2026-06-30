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
from typing import Annotated, Any

import fastmcp
from pydantic import Field

from teardrop._meta import APP_VERSION
from teardrop.config import get_settings
from tools import registry

logger = logging.getLogger(__name__)

MCP_SERVER_DESCRIPTION = (
    "The native infrastructure layer for autonomous economic agents. "
    "Teardrop exposes its curated Web3, data, and utility tools "
    "over MCP with public discovery and authenticated execution."
)


def _signature_annotation_for_field(field_info: Any) -> Any:
    base_annotation = field_info.annotation if field_info.annotation is not None else Any
    metadata = list(field_info.metadata)
    field_kwargs: dict[str, Any] = {}

    if field_info.description is not None:
        field_kwargs["description"] = field_info.description
    if field_info.title is not None:
        field_kwargs["title"] = field_info.title
    if field_info.examples is not None:
        field_kwargs["examples"] = field_info.examples
    if field_info.json_schema_extra is not None:
        field_kwargs["json_schema_extra"] = field_info.json_schema_extra
    if field_info.deprecated is not None:
        field_kwargs["deprecated"] = field_info.deprecated

    if field_kwargs:
        metadata.append(Field(**field_kwargs))

    if not metadata:
        return base_annotation
    return Annotated[(base_annotation, *metadata)]


def _signature_default_for_field(field_info: Any) -> Any:
    if field_info.is_required():
        return inspect.Parameter.empty
    if field_info.default_factory is not None:
        return field_info.default_factory()
    return field_info.default


# ─── Build FastMCP server ─────────────────────────────────────────────────────

s = get_settings()

mcp = fastmcp.FastMCP(
    name="Teardrop",
    version=APP_VERSION,
    instructions=MCP_SERVER_DESCRIPTION,
    website_url=s.app_base_url if s.app_base_url else None,
    icons=[{"src": s.agent_card_icon_url}] if s.agent_card_icon_url else None,
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
                    annotation=_signature_annotation_for_field(fi),
                    default=_signature_default_for_field(fi),
                )
                for name, fi in schema.model_fields.items()
            ]
            handler.__signature__ = inspect.Signature(params)
            handler.__annotations__ = {p.name: p.annotation for p in params if p.annotation is not inspect.Parameter.empty}
            return handler

        handler = _make_handler(implementation, input_schema)
        handler.__name__ = f"mcp_{name}"
        handler.__doc__ = description

        mcp.tool(
            name=name,
            description=description,
            title=tool_def.get("title"),
            output_schema=tool_def.get("output_schema"),
            annotations=tool_def.get("annotations"),
        )(handler)
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
