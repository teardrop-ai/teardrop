# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Web-search tool – Tavily-backed with stub fallback."""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)


# ─── Schemas ──────────────────────────────────────────────────────────────────


class WebSearchInput(BaseModel):
    query: str = Field(..., description="Search query", max_length=500)
    num_results: int = Field(default=5, ge=1, le=20)
    search_depth: Literal["basic", "advanced"] = Field(
        default="basic",
        description="'basic' for fast results, 'advanced' for thorough research (higher cost)",
    )
    topic: Literal["general", "news", "finance"] = Field(
        default="general",
        description="Search topic: 'general' for web, 'news' for headlines, 'finance' for markets",
    )


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    score: float | None = None


class WebSearchOutput(BaseModel):
    query: str
    num_results: int
    results: list[SearchResult]
    note: str | None = None


# ─── Implementation ──────────────────────────────────────────────────────────


async def web_search(
    query: str,
    num_results: int = 5,
    search_depth: Literal["basic", "advanced"] = "basic",
    topic: Literal["general", "news", "finance"] = "general",
) -> dict[str, Any]:
    """Search the web via Tavily.  Falls back to a stub when no API key is set."""
    from config import get_settings

    settings = get_settings()
    api_key = settings.tavily_api_key

    if api_key:
        return await _tavily_search(query, num_results, api_key, search_depth, topic)

    logger.warning("web_search: no TAVILY_API_KEY set – returning stub results")
    return _stub_results(query, num_results)


async def _tavily_search(
    query: str,
    num_results: int,
    api_key: str,
    search_depth: str = "basic",
    topic: str = "general",
) -> dict[str, Any]:
    """Call the Tavily search API with error handling."""
    from tavily import AsyncTavilyClient

    try:
        client = AsyncTavilyClient(api_key=api_key)
        response = await client.search(
            query=query,
            max_results=num_results,
            search_depth=search_depth,
            topic=topic,
        )

        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "score": r.get("score"),
            }
            for r in response.get("results", [])
        ]
        return {"query": query, "num_results": num_results, "results": results}
    except Exception as exc:
        logger.warning("Tavily search failed: %s", exc)
        return {
            "query": query,
            "num_results": 0,
            "results": [],
            "note": f"Search unavailable: {type(exc).__name__}",
        }


def _stub_results(query: str, num_results: int) -> dict[str, Any]:
    """Return placeholder results when no search provider is configured."""
    return {
        "query": query,
        "num_results": num_results,
        "results": [
            {
                "title": "Placeholder result – configure TAVILY_API_KEY",
                "url": "https://example.com",
                "snippet": ("This is a stub result. Set TAVILY_API_KEY in .env to get live search results."),
                "score": None,
            }
        ],
        "note": "web_search is running in stub mode. Set TAVILY_API_KEY for live results.",
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="web_search",
    version="1.0.0",
    description=(
        "Real-time web search via Tavily. Use for current events, fact-checking, and research. "
        "Set search_depth='advanced' for complex research queries (higher quality, higher cost). "
        "Set topic='news' for recent headlines or 'finance' for market information."
    ),
    tags=["search", "web", "realtime"],
    input_schema=WebSearchInput,
    output_schema=WebSearchOutput,
    implementation=web_search,
)
