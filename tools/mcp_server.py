"""Standalone FastMCP server exposing Teardrop tools over MCP protocol.

Run independently for tool discovery and reuse across multiple agents:
    python tools/mcp_server.py

The server listens on stdio by default (suitable for Claude Desktop / MCP clients).
Pass --transport=sse to expose via HTTP SSE instead.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import fastmcp

from tools.mcp_tools import (
    CalculateInput,
    GetDatetimeInput,
    SummarizeTextInput,
    WebSearchInput,
    calculate,
    get_datetime,
    summarize_text,
    web_search,
)

logger = logging.getLogger(__name__)

# ─── Build FastMCP server ─────────────────────────────────────────────────────

mcp = fastmcp.FastMCP(
    name="teardrop-tools",
    version="1.0.0",
    instructions=(
        "Teardrop MCP tool server. Provides arithmetic calculation, "
        "datetime lookup, web search, and text summarization tools."
    ),
)


@mcp.tool(description="Evaluate a safe arithmetic expression (supports +,-,*,/,**,%,sqrt,abs,etc.)")
async def mcp_calculate(expression: str) -> dict[str, Any]:
    inp = CalculateInput(expression=expression)
    return await calculate(inp.expression)


@mcp.tool(description="Return current UTC date and time. Optional strftime format.")
async def mcp_get_datetime(format: str = "%Y-%m-%d %H:%M:%S UTC") -> dict[str, str]:
    inp = GetDatetimeInput(format=format)
    return await get_datetime(inp.format)


@mcp.tool(description="Search the web. Returns titles, URLs and snippets (stub until API key set).")
async def mcp_web_search(query: str, num_results: int = 5) -> dict[str, Any]:
    inp = WebSearchInput(query=query, num_results=num_results)
    return await web_search(inp.query, inp.num_results)


@mcp.tool(description="Return word/sentence/paragraph statistics for a given text.")
async def mcp_summarize_text(text: str) -> dict[str, Any]:
    inp = SummarizeTextInput(text=text)
    return await summarize_text(inp.text)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    transport = "stdio"
    for arg in sys.argv[1:]:
        if arg.startswith("--transport="):
            transport = arg.split("=", 1)[1]

    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Teardrop MCP server (transport=%s)", transport)
    asyncio.run(mcp.run_async(transport=transport))
