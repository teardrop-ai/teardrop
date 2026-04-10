# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Tool definitions package – auto-registers all tools with the registry."""

from __future__ import annotations

from tools.definitions.calculate import TOOL as calculate_tool
from tools.definitions.convert_currency import TOOL as convert_currency_tool
from tools.definitions.decode_transaction import TOOL as decode_transaction_tool
from tools.definitions.get_block import TOOL as get_block_tool
from tools.definitions.get_datetime import TOOL as get_datetime_tool
from tools.definitions.get_erc20_balance import TOOL as get_erc20_balance_tool
from tools.definitions.get_eth_balance import TOOL as get_eth_balance_tool
from tools.definitions.get_gas_price import TOOL as get_gas_price_tool
from tools.definitions.get_token_price import TOOL as get_token_price_tool
from tools.definitions.get_transaction import TOOL as get_transaction_tool
from tools.definitions.get_wallet_portfolio import TOOL as get_wallet_portfolio_tool
from tools.definitions.http_fetch import TOOL as http_fetch_tool
from tools.definitions.read_contract import TOOL as read_contract_tool
from tools.definitions.resolve_ens import TOOL as resolve_ens_tool
from tools.definitions.summarize_text import TOOL as count_text_stats_tool
from tools.definitions.web_search import TOOL as web_search_tool
from tools.registry import ToolRegistry

_ALL_TOOLS = [
    calculate_tool,
    convert_currency_tool,
    decode_transaction_tool,
    get_datetime_tool,
    get_block_tool,
    get_gas_price_tool,
    get_token_price_tool,
    web_search_tool,
    count_text_stats_tool,
    http_fetch_tool,
    get_eth_balance_tool,
    get_erc20_balance_tool,
    get_transaction_tool,
    get_wallet_portfolio_tool,
    read_contract_tool,
    resolve_ens_tool,
]


def register_all(registry: ToolRegistry) -> None:
    """Register every tool definition with the given registry."""
    for tool in _ALL_TOOLS:
        registry.register(tool)
