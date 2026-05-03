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
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ─── Global semaphore instance ────────────────────────────────────────────────

_semaphore: asyncio.Semaphore | None = None
_chain_semaphores: dict[int, asyncio.Semaphore] = {}


@dataclass
class _TokenBucket:
    tokens: float
    capacity: float
    rate: float
    last_refill: float


_chain_rate_limiters: dict[int, _TokenBucket] = {}
_chain_rate_locks: dict[int, asyncio.Lock] = {}
_chain_cooldown_until: dict[int, float] = {}

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


def init_chain_rate_limiter(chain_id: int, rps: float) -> None:
    """Initialize or reset a per-chain token-bucket rate limiter."""
    safe_rps = max(0.1, float(rps))
    now = time.monotonic()
    _chain_rate_limiters[chain_id] = _TokenBucket(
        tokens=safe_rps,
        capacity=safe_rps,
        rate=safe_rps,
        last_refill=now,
    )
    _chain_rate_locks[chain_id] = asyncio.Lock()
    _chain_cooldown_until.pop(chain_id, None)
    logger.info("RPC chain rate limiter initialized for chain_id=%d with rps=%.2f", chain_id, safe_rps)


def set_chain_cooldown(chain_id: int, cooldown_seconds: float) -> None:
    """Apply a shared cooldown window for one chain after a provider 429."""
    if cooldown_seconds <= 0:
        return
    now_mono = time.monotonic()
    proposed_until = now_mono + cooldown_seconds
    _chain_cooldown_until[chain_id] = max(_chain_cooldown_until.get(chain_id, 0.0), proposed_until)


def get_chain_cooldown_wait(chain_id: int) -> float:
    """Return remaining shared cooldown wait time for one chain."""
    return max(0.0, _chain_cooldown_until.get(chain_id, 0.0) - time.monotonic())


async def _acquire_chain_rate_token(chain_id: int) -> None:
    """Consume one token from the per-chain bucket, waiting if depleted."""
    bucket = _chain_rate_limiters.get(chain_id)
    if bucket is None:
        return

    lock = _chain_rate_locks.setdefault(chain_id, asyncio.Lock())

    while True:
        async with lock:
            now = time.monotonic()
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.last_refill = now
            bucket.tokens = min(bucket.capacity, bucket.tokens + (elapsed * bucket.rate))

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return

            wait_seconds = max(0.01, (1.0 - bucket.tokens) / bucket.rate)

        logger.info("RPC rate limiter: sleeping %.2fs for chain_id=%d", wait_seconds, chain_id)
        await asyncio.sleep(wait_seconds)


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

    cooldown_wait = get_chain_cooldown_wait(chain_id)
    if cooldown_wait > 0:
        logger.info("RPC shared cooldown: sleeping %.2fs for chain_id=%d", cooldown_wait, chain_id)
        await asyncio.sleep(cooldown_wait)

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

        await _acquire_chain_rate_token(chain_id)
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
