# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Global RPC semaphore to limit concurrent Web3 eth_calls across all agent runs.

Prevents organizational RPC saturation. Public JSON-RPC providers enforce per-account
rate limits (typically 5–10 concurrent calls); we use a global semaphore at the app level
to prevent thundering herd under high concurrency.

Initialize once at app startup via `init_rpc_semaphore(config)`.
Acquire at the shared RPC helper boundary (``rpc_call``) so each individual
JSON-RPC attempt is throttled uniformly across tools.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# ─── Global semaphore instance ────────────────────────────────────────────────

_semaphore: asyncio.Semaphore | None = None
_chain_semaphores: dict[int, asyncio.Semaphore] = {}

# ─── Global diagnostics counters ──────────────────────────────────────────────

_total_acquisitions = 0
_total_wait_ms = 0
_max_wait_ms = 0


def init_rpc_semaphore(limit: int) -> None:
    """Initialize the global RPC semaphore. Called once at app startup.

    Args:
        limit: Maximum concurrent RPC calls (e.g., config.agent_rpc_semaphore_limit).
    """
    global _semaphore
    _semaphore = asyncio.Semaphore(limit)
    logger.info("RPC semaphore initialized with limit=%d", limit)


def init_chain_semaphore(chain_id: int, limit: int) -> None:
    """Initialize or reset the per-chain semaphore for a specific chain."""
    _chain_semaphores[chain_id] = asyncio.Semaphore(limit)
    logger.info("RPC chain semaphore initialized for chain_id=%d with limit=%d", chain_id, limit)


@asynccontextmanager
async def acquire_rpc_semaphore():
    """Context manager to acquire and release the global RPC semaphore.

    Logs acquisition wait times and contention metrics.
    Ensures all RPC-bound tool calls respect the global concurrency limit.

    Example:
        async with acquire_rpc_semaphore():
            result = await web3.eth.get_balance(address)
    """
    global _total_acquisitions, _total_wait_ms, _max_wait_ms

    if _semaphore is None:
        raise RuntimeError("RPC semaphore not initialized. Call init_rpc_semaphore(limit) at app startup.")

    start_mono = time.monotonic()
    logger.debug("Acquiring RPC semaphore (current permits: %d)", _semaphore._value)
    async with _semaphore:
        wait_ms = int((time.monotonic() - start_mono) * 1000)
        _total_acquisitions += 1
        _total_wait_ms += wait_ms
        _max_wait_ms = max(_max_wait_ms, wait_ms)

        if wait_ms > 100:  # Only log significant waits at INFO
            logger.info("RPC semaphore contention: waited %dms (permits remaining: %d)", wait_ms, _semaphore._value)
        else:
            logger.debug("RPC semaphore acquired in %dms (permits remaining: %d)", wait_ms, _semaphore._value)

        # Log summary every 50 acquisitions
        if _total_acquisitions % 50 == 0:
            avg_wait = _total_wait_ms / _total_acquisitions
            logger.info(
                "RPC semaphore summary: total=%d, avg_wait=%.1fms, max_wait=%dms, permits_available=%d",
                _total_acquisitions,
                avg_wait,
                _max_wait_ms,
                _semaphore._value,
            )

        try:
            yield
        finally:
            logger.debug("RPC semaphore released (permits available: %d)", _semaphore._value)


@asynccontextmanager
async def acquire_chain_semaphore(chain_id: int | None):
    """Acquire a per-chain RPC semaphore when a chain id is provided.

    If no semaphore is registered for the chain (or chain_id is None), this
    context manager becomes a no-op to preserve backwards compatibility.
    """
    if chain_id is None:
        yield
        return

    sem = _chain_semaphores.get(chain_id)
    if sem is None:
        yield
        return

    start_mono = time.monotonic()
    logger.debug("Acquiring RPC chain semaphore for chain_id=%d (permits: %d)", chain_id, sem._value)
    async with sem:
        wait_ms = int((time.monotonic() - start_mono) * 1000)
        if wait_ms > 100:
            logger.info(
                "RPC chain semaphore contention: chain_id=%d waited %dms (permits remaining: %d)",
                chain_id,
                wait_ms,
                sem._value,
            )
        else:
            logger.debug(
                "RPC chain semaphore acquired: chain_id=%d in %dms (permits remaining: %d)",
                chain_id,
                wait_ms,
                sem._value,
            )
        yield


def get_rpc_semaphore() -> asyncio.Semaphore:
    """Get the global RPC semaphore. Raises RuntimeError if not initialized."""
    if _semaphore is None:
        raise RuntimeError("RPC semaphore not initialized. Call init_rpc_semaphore(limit) at app startup.")
    return _semaphore


def get_chain_semaphore(chain_id: int) -> asyncio.Semaphore:
    """Get a registered chain semaphore. Raises RuntimeError if missing."""
    sem = _chain_semaphores.get(chain_id)
    if sem is None:
        raise RuntimeError(f"RPC chain semaphore not initialized for chain_id={chain_id}.")
    return sem
