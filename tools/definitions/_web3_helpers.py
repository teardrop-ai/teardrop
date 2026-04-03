# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Shared Web3 provider factory for on-chain tools."""

from __future__ import annotations

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from config import get_settings

_CHAIN_MAP: dict[int, str] = {}


def _get_rpc_url(chain_id: int) -> str:
    """Return the RPC URL for the given chain ID."""
    settings = get_settings()
    urls: dict[int, str | None] = {
        1: settings.ethereum_rpc_url,
        8453: settings.base_rpc_url,
    }
    url = urls.get(chain_id)
    if not url:
        raise ValueError(f"Unsupported or unconfigured chain_id={chain_id}")
    return url


def get_web3(chain_id: int = 1) -> AsyncWeb3:
    """Return an AsyncWeb3 instance for the given chain."""
    rpc_url = _get_rpc_url(chain_id)
    return AsyncWeb3(AsyncHTTPProvider(rpc_url))
