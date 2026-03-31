"""Tool definitions package – auto-registers all tools with the registry."""

from __future__ import annotations

from tools.registry import ToolRegistry

from tools.definitions.calculate import TOOL as calculate_tool
from tools.definitions.get_datetime import TOOL as get_datetime_tool
from tools.definitions.web_search import TOOL as web_search_tool
from tools.definitions.summarize_text import TOOL as summarize_text_tool

_ALL_TOOLS = [
    calculate_tool,
    get_datetime_tool,
    web_search_tool,
    summarize_text_tool,
]


def register_all(registry: ToolRegistry) -> None:
    """Register every tool definition with the given registry."""
    for tool in _ALL_TOOLS:
        registry.register(tool)
