# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared aiohttp.ClientSession helper for tool definitions.

Per the aiohttp docs ("Don't create a session per request"), reusing a single
``ClientSession`` across calls keeps the underlying connection pool warm,
preserves DNS caching, and re-uses TLS sessions — saving ~100–300 ms per call
on cold connections compared with the per-request ``async with ClientSession()``
pattern.

Sessions are bound to the running event loop, so we lazy-init per loop and
expose a ``close_http_sessions()`` coroutine for the FastAPI lifespan to call
at shutdown.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)


_coingecko_session: aiohttp.ClientSession | None = None
_session_lock: asyncio.Lock | None = None


def _get_session_lock() -> asyncio.Lock:
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock


async def get_coingecko_session() -> aiohttp.ClientSession:
    """Return a shared aiohttp.ClientSession for CoinGecko calls.

    The session reuses connections (TCPConnector limit=20) and caches DNS
    lookups for 5 minutes, which is more than adequate for CoinGecko's
    handful of host names.
    """
    global _coingecko_session
    if _coingecko_session is not None and not _coingecko_session.closed:
        return _coingecko_session

    async with _get_session_lock():
        if _coingecko_session is not None and not _coingecko_session.closed:
            return _coingecko_session
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        _coingecko_session = aiohttp.ClientSession(connector=connector)
        logger.debug("Initialised shared CoinGecko aiohttp session")
        return _coingecko_session


async def close_http_sessions() -> None:
    """Close all shared aiohttp sessions. Safe to call multiple times."""
    global _coingecko_session
    if _coingecko_session is not None and not _coingecko_session.closed:
        try:
            await _coingecko_session.close()
        except Exception as exc:  # pragma: no cover — best-effort shutdown
            logger.warning("Error closing CoinGecko session: %s", exc)
    _coingecko_session = None
