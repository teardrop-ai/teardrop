# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Global RPC semaphore to limit concurrent Web3 eth_calls across all agent runs.

Prevents organizational RPC saturation. Public JSON-RPC providers enforce per-account
rate limits (typically 5–10 concurrent calls); we use a global semaphore at the app level
to prevent thundering herd under high concurrency.

Initialize once at app startup via `init_rpc_semaphore(config)`.
Acquire within tool implementations via `async with acquire_rpc_semaphore():`.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# ─── Global semaphore instance ────────────────────────────────────────────────

_semaphore: asyncio.Semaphore | None = None


def init_rpc_semaphore(limit: int) -> None:
    """Initialize the global RPC semaphore. Called once at app startup.

    Args:
        limit: Maximum concurrent RPC calls (e.g., config.agent_rpc_semaphore_limit).
    """
    global _semaphore
    _semaphore = asyncio.Semaphore(limit)
    logger.info("RPC semaphore initialized with limit=%d", limit)


@asynccontextmanager
async def acquire_rpc_semaphore():
    """Context manager to acquire and release the global RPC semaphore.

    Logs at DEBUG level when acquired/released for observability.
    Ensures all RPC-bound tool calls respect the global concurrency limit.

    Example:
        async with acquire_rpc_semaphore():
            result = await web3.eth.get_balance(address)
    """
    if _semaphore is None:
        raise RuntimeError(
            "RPC semaphore not initialized. Call init_rpc_semaphore(limit) at app startup."
        )

    logger.debug("Acquiring RPC semaphore (current permits: %d)", _semaphore._value)
    async with _semaphore:
        logger.debug("RPC semaphore acquired (permits remaining: %d)", _semaphore._value)
        try:
            yield
        finally:
            logger.debug("RPC semaphore released (permits available: %d)", _semaphore._value)


def get_rpc_semaphore() -> asyncio.Semaphore:
    """Get the global RPC semaphore. Raises RuntimeError if not initialized."""
    if _semaphore is None:
        raise RuntimeError(
            "RPC semaphore not initialized. Call init_rpc_semaphore(limit) at app startup."
        )
    return _semaphore
