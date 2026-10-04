# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Standalone MCP server exposing Teardrop tools over MCP protocol.

Run independently for tool discovery and reuse across multiple agents:
    python tools/mcp_server.py

The server listens on stdio by default (suitable for Claude Desktop / MCP clients).
Pass --transport=sse to expose via HTTP SSE instead.

Tools are auto-registered from the ToolRegistry — adding a new ToolDefinition
in tools/definitions/ will automatically expose it here.
"""

from __future__ import annotations

import inspect
import logging
import sys
from collections.abc import Iterable
from typing import Annotated, Any

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from teardrop._meta import APP_VERSION
from teardrop.config import get_settings
from tools import registry
from tools.capabilities import (
    Capability,
    build_capability_manifest,
    capability_meta,
    get_capability_manifest,
    set_capability_manifest,
)
from tools.registry import format_mcp_quality_description
from tools.schema import build_pydantic_model

logger = logging.getLogger(__name__)
# Qualified `org/tool` names are billing keys; SEP-986 would warn on every refresh.
logging.getLogger("mcp.shared.tool_name_validation").setLevel(logging.ERROR)

# Parity with the deprecated /mcp/v1 tools/list page size.
_COMMUNITY_TOOL_LIMIT = 200

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


# ─── Build MCP server ─────────────────────────────────────────────────────────


def _make_handler(impl: Any, schema: Any, result_model: Any, *, exclude_none: bool = False) -> Any:
    async def handler(**kwargs: Any) -> Any:
        validated = schema(**kwargs)
        return await impl(**validated.model_dump(exclude_none=exclude_none))

    # Inject an explicit typed signature from the Pydantic model so
    # MCPServer builds its JSON schema by inspecting __signature__.
    params = [
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=_signature_annotation_for_field(fi),
            default=_signature_default_for_field(fi),
        )
        for name, fi in schema.model_fields.items()
    ]
    handler.__signature__ = inspect.Signature(
        params,
        return_annotation=result_model if result_model is not None else inspect.Signature.empty,
    )
    handler.__annotations__ = {p.name: p.annotation for p in params if p.annotation is not inspect.Parameter.empty}
    if result_model is not None:
        handler.__annotations__["return"] = result_model
    return handler


def _register_tools_with_mcp(
    server: MCPServer,
    *,
    reputation: dict[str, dict[str, Any]] | None = None,
    prices: dict[str, int] | None = None,
    replace_existing: bool = False,
) -> None:
    """Auto-register all active tools from the registry with MCPServer."""
    for tool_def in registry.to_mcp_tool_defs(reputation, prices):
        name = tool_def["name"]
        description = tool_def["description"]
        input_schema = tool_def["input_schema"]
        output_model = tool_def["output_model"]
        implementation = tool_def["implementation"]

        if replace_existing:
            server.remove_tool(name)

        handler = _make_handler(implementation, input_schema, output_model)
        handler.__name__ = f"mcp_{name}"
        handler.__doc__ = description

        server.tool(
            name=name,
            description=description,
            title=tool_def.get("title"),
            annotations=tool_def.get("annotations"),
            meta=tool_def.get("meta"),
        )(handler)
        logger.debug("MCP: registered tool %s", name)


def _marketplace_impl(qualified_name: str) -> Any:
    org_slug, tool_name = qualified_name.split("/", 1)

    async def impl(**arguments: Any) -> Any:
        from marketplace import get_marketplace_tool_by_name
        from marketplace.execution import execute_marketplace_tool

        # Re-read per call so unpublished or deactivated tools stop immediately.
        tool_row = await get_marketplace_tool_by_name(tool_name, org_slug)
        if tool_row is None:
            raise ToolError(f"Tool not found: {qualified_name}")
        result = await execute_marketplace_tool(tool_row, arguments)
        # An error result must surface as isError so the gateway never settles it.
        if isinstance(result, dict) and "error" in result:
            raise ToolError(str(result["error"]))
        return result

    return impl


async def _sync_community_tools(server: MCPServer, capabilities: Iterable[Capability]) -> None:
    listed = {capability.name: capability for capability in capabilities if capability.kind == "community"}
    for name in [tool.name for tool in await server.list_tools() if "/" in tool.name]:
        server.remove_tool(name)
    for name, capability in listed.items():
        try:
            schema = build_pydantic_model(name, capability.input_schema, model_name=f"MPTool_{name.replace('/', '_')}_Input")
            handler = _make_handler(_marketplace_impl(name), schema, None, exclude_none=True)
            handler.__name__ = f"mcp_{name.replace('/', '__')}"
            server.tool(
                name=name,
                description=format_mcp_quality_description(capability.description, capability.reputation),
                meta=capability_meta(capability),
            )(handler)
        except Exception:
            logger.warning("MCP: skipped community tool %s (unsupported input schema)", name)


async def refresh_mcp_tools(server: MCPServer) -> None:
    """Refresh platform reputation/price meta and the published community tool set.

    Only the app lifespan calls this, behind the billing gateway; the standalone
    stdio server never exposes community tools.
    """
    from billing import get_current_pricing, get_tool_pricing_overrides, resolve_tool_cost
    from marketplace import get_marketplace_catalog
    from marketplace.reputation import get_public_reputation

    try:
        reputation = await get_public_reputation()
    except Exception:
        logger.warning("MCP: reputation refresh unavailable")
        return

    marketplace_enabled = get_settings().marketplace_enabled
    try:
        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0
        prices = {
            tool.name: await resolve_tool_cost(tool.name, overrides, default_cost, marketplace_enabled)
            for tool in registry.list_latest()
        }
        catalog = (
            await get_marketplace_catalog(overrides, default_cost, limit=_COMMUNITY_TOOL_LIMIT) if marketplace_enabled else []
        )
    except Exception:
        # Price meta is advisory (the gateway re-prices every call); keep the last community set.
        logger.warning("MCP: tool pricing refresh unavailable")
        kept_community = tuple(c for c in get_capability_manifest() if c.kind == "community")
        set_capability_manifest(build_capability_manifest(reputation) + kept_community)
        _register_tools_with_mcp(server, reputation=reputation, replace_existing=True)
        return

    capabilities = build_capability_manifest(reputation, prices, catalog)
    set_capability_manifest(capabilities)
    _register_tools_with_mcp(server, reputation=reputation, prices=prices, replace_existing=True)
    await _sync_community_tools(server, capabilities)


def create_mcp_server() -> MCPServer:
    """Create an isolated official MCP server with Teardrop tools."""
    settings = get_settings()
    server = MCPServer(
        name="Teardrop",
        instructions=MCP_SERVER_DESCRIPTION,
        website_url=settings.app_base_url if settings.app_base_url else None,
        icons=[{"src": settings.agent_card_icon_url}] if settings.agent_card_icon_url else None,
        version=APP_VERSION,
        # 2026-07-28 freshness hints. The list is auth-context-specific (community
        # tools are Bearer-only), so scope stays private; 60s sits well under the
        # 300s reputation/price refresh.
        cache_hints={"tools/list": CacheHint(ttl_ms=60_000, scope="private")},
    )
    _register_tools_with_mcp(server)
    return server


def build_mcp_app(server: MCPServer) -> Any:
    """Build the mounted stateless Streamable HTTP application."""
    return server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


mcp = create_mcp_server()

# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for teardrop-mcp command."""
    transport = "stdio"
    for arg in sys.argv[1:]:
        if arg.startswith("--transport="):
            transport = arg.split("=", 1)[1]

    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Teardrop MCP server (transport=%s)", transport)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
