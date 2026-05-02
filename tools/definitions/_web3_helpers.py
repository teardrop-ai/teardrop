# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared Web3 provider factory for on-chain tools."""

from __future__ import annotations

import asyncio
import logging
import random

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from config import get_settings
from tools.definitions._rpc_semaphore import acquire_rpc_semaphore

logger = logging.getLogger(__name__)

_CHAIN_MAP: dict[int, str] = {}

# Cache of AsyncWeb3 instances keyed by (event_loop_id, chain_id). Reused
# across calls to avoid leaking the underlying aiohttp ClientSession that
# AsyncHTTPProvider creates lazily on first request.
_web3_cache: dict[tuple[int, int], AsyncWeb3] = {}

# ─── Retry-capable provider ───────────────────────────────────────────────────
# Public and free-tier RPC endpoints (Alchemy, Infura, QuickNode free plans)
# enforce per-second call budgets. When multiple tools fan out parallel eth_call
# requests to the same chain simultaneously, a 429 is common. This subclass
# retries up to _RETRY_MAX times with exponential backoff before propagating,
# giving the RPC time to recover without wasting an agent iteration.

_RETRY_MAX = 2
_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt → 1.0 s, 2.0 s
_RETRY_JITTER_RATIO = 0.30
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate-limited",
    "too many requests",
    "exceeded",
    "throttl",
    "quanta",
)


class _RetryAsyncHTTPProvider(AsyncHTTPProvider):
    """AsyncHTTPProvider with transparent exponential-backoff retry on 429s."""

    async def make_request(self, method: str, params: list) -> dict:  # type: ignore[override]
        for attempt in range(_RETRY_MAX + 1):
            try:
                return await super().make_request(method, params)  # type: ignore[return-value]
            except Exception as exc:
                err_lower = str(exc).lower()
                if attempt < _RETRY_MAX and any(m in err_lower for m in _RATE_LIMIT_MARKERS):
                    delay = _RETRY_BASE_DELAY * (2**attempt)
                    jitter = delay * random.uniform(-_RETRY_JITTER_RATIO, _RETRY_JITTER_RATIO)
                    sleep_for = max(0.0, delay + jitter)
                    logger.debug(
                        "RPC 429 on attempt %d/%d for %s; retrying in %.2fs",
                        attempt + 1,
                        _RETRY_MAX,
                        method,
                        sleep_for,
                    )
                    await asyncio.sleep(sleep_for)
                    continue
                raise
        raise RuntimeError("unreachable")  # pragma: no cover


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
    """Return a cached AsyncWeb3 instance for the given chain.

    Instances are cached per (event-loop, chain_id) tuple. Each
    ``_RetryAsyncHTTPProvider`` creates an internal ``aiohttp.ClientSession``
    that lives for the lifetime of the provider; reusing the same provider
    object preserves the connection pool, keeps DNS warm, and — critically —
    avoids the "Unclosed client session" warnings emitted when transient
    AsyncWeb3 objects get garbage-collected.

    The cache is keyed by the running event loop because aiohttp sessions are
    bound to the loop they were created on; under pytest's per-test loops we
    must not reuse a session across loops.
    """
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = 0

    key = (loop_id, chain_id)
    cached = _web3_cache.get(key)
    if cached is not None:
        return cached

    rpc_url = _get_rpc_url(chain_id)
    w3 = AsyncWeb3(_RetryAsyncHTTPProvider(rpc_url))
    _web3_cache[key] = w3
    return w3


async def close_web3_clients() -> None:
    """Close all cached AsyncWeb3 client sessions. Safe to call multiple times.

    Intended for FastAPI lifespan shutdown. Best-effort: a single failure
    will not prevent other sessions from closing.
    """
    global _web3_cache
    pending = list(_web3_cache.values())
    _web3_cache = {}
    for w3 in pending:
        provider = getattr(w3, "provider", None)
        # web3.py's AsyncHTTPProvider exposes the aiohttp session as
        # ``cached_session`` (private but stable) once the first request has
        # initialised it. ``disconnect`` is the public coroutine on newer
        # versions; fall back to closing the session directly otherwise.
        try:
            if hasattr(provider, "disconnect"):
                await provider.disconnect()
                continue
        except Exception as exc:  # pragma: no cover — best-effort shutdown
            logger.warning("Error disconnecting web3 provider: %s", exc)
        session = getattr(provider, "cached_session", None)
        if session is not None and not getattr(session, "closed", True):
            try:
                await session.close()
            except Exception as exc:  # pragma: no cover — best-effort shutdown
                logger.warning("Error closing web3 aiohttp session: %s", exc)


async def rpc_call(coro_fn, timeout_seconds: int | None = None):
    """Wrap a Web3 contract/RPC call with timeout and rate-limit resilience.

    Accepts a *callable* (zero-argument function or lambda) that returns a fresh
    coroutine on each invocation.  Python coroutines are one-shot objects —
    they cannot be re-awaited after raising an exception (RuntimeError).
    Requiring a callable ensures a fresh coroutine is created for every retry
    attempt.

    Args:
        coro_fn: Callable returning a coroutine.
            e.g. ``lambda: contract.functions.method().call()``
        timeout_seconds: Per-attempt timeout in seconds (defaults to
            config.agent_rpc_call_timeout_seconds).

    Returns:
        The result of the coroutine.

    Raises:
        asyncio.TimeoutError: If an attempt exceeds the timeout.
        Exception: The underlying RPC exception if all retries fail.
    """
    if timeout_seconds is None:
        timeout_seconds = get_settings().agent_rpc_call_timeout_seconds

    for attempt in range(_RETRY_MAX + 1):
        if attempt > 0:
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            # Backoff sleep intentionally occurs outside semaphore acquisition.
            jitter = delay * random.uniform(-_RETRY_JITTER_RATIO, _RETRY_JITTER_RATIO)
            await asyncio.sleep(max(0.0, delay + jitter))
        try:
            async with acquire_rpc_semaphore():
                return await asyncio.wait_for(coro_fn(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.debug("RPC call timed out after %ds", timeout_seconds)
            raise
        except Exception as exc:
            err_lower = str(exc).lower()
            if attempt < _RETRY_MAX and any(m in err_lower for m in _RATE_LIMIT_MARKERS):
                logger.warning(
                    "JSON-RPC rate limit on attempt %d/%d; retrying in %.2fs. Error: %s",
                    attempt + 1,
                    _RETRY_MAX,
                    _RETRY_BASE_DELAY * (2**attempt),
                    err_lower,
                )
                continue
            raise
