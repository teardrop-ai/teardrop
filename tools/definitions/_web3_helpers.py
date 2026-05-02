# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared Web3 provider factory for on-chain tools."""

from __future__ import annotations

import asyncio
import logging

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from config import get_settings
from tools.definitions._rpc_semaphore import acquire_rpc_semaphore

logger = logging.getLogger(__name__)

_CHAIN_MAP: dict[int, str] = {}

# ─── Retry-capable provider ───────────────────────────────────────────────────
# Public and free-tier RPC endpoints (Alchemy, Infura, QuickNode free plans)
# enforce per-second call budgets. When multiple tools fan out parallel eth_call
# requests to the same chain simultaneously, a 429 is common. This subclass
# retries up to _RETRY_MAX times with exponential backoff before propagating,
# giving the RPC time to recover without wasting an agent iteration.

_RETRY_MAX = 2
_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt → 1.0 s, 2.0 s
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
                    logger.debug(
                        "RPC 429 on attempt %d/%d for %s; retrying in %.2fs",
                        attempt + 1,
                        _RETRY_MAX,
                        method,
                        delay,
                    )
                    await asyncio.sleep(delay)
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
    """Return an AsyncWeb3 instance for the given chain."""
    rpc_url = _get_rpc_url(chain_id)
    return AsyncWeb3(_RetryAsyncHTTPProvider(rpc_url))


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
            await asyncio.sleep(delay)
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
