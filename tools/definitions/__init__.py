"""Tool definitions package – auto-registers all tools with the registry."""

from __future__ import annotations

from tools.registry import ToolRegistry

from tools.definitions.calculate import TOOL as calculate_tool
from tools.definitions.get_datetime import TOOL as get_datetime_tool
from tools.definitions.web_search import TOOL as web_search_tool
from tools.definitions.summarize_text import TOOL as summarize_text_tool
from tools.definitions.get_eth_balance import TOOL as get_eth_balance_tool
from tools.definitions.get_erc20_balance import TOOL as get_erc20_balance_tool
from tools.definitions.get_transaction import TOOL as get_transaction_tool
from tools.definitions.resolve_ens import TOOL as resolve_ens_tool
from tools.definitions.get_block import TOOL as get_block_tool

_ALL_TOOLS = [
    calculate_tool,
    get_datetime_tool,
    web_search_tool,
    summarize_text_tool,
    get_eth_balance_tool,
    get_erc20_balance_tool,
    get_transaction_tool,
    resolve_ens_tool,
    get_block_tool,
]


def register_all(registry: ToolRegistry) -> None:
    """Register every tool definition with the given registry."""
    for tool in _ALL_TOOLS:
        registry.register(tool)
