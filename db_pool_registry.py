# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared registry for module-owned asyncpg pools."""

from __future__ import annotations

import asyncpg

_POOLS: dict[str, asyncpg.Pool] = {}


def bind_pool(scope: str, pool: asyncpg.Pool) -> asyncpg.Pool:
    """Register and return a pool for the given scope."""
    _POOLS[scope] = pool
    return pool


def unbind_pool(scope: str) -> None:
    """Remove a pool binding for the given scope."""
    _POOLS.pop(scope, None)


def require_pool(scope: str, local_pool: asyncpg.Pool | None, error_message: str) -> asyncpg.Pool:
    """Return the module's local pool and keep the shared registry in sync."""
    if local_pool is None:
        raise RuntimeError(error_message)
    if _POOLS.get(scope) is not local_pool:
        _POOLS[scope] = local_pool
    return local_pool


def get_bound_pool(scope: str) -> asyncpg.Pool | None:
    """Return a bound pool for a scope when present."""
    return _POOLS.get(scope)


def clear_all_bound_pools() -> None:
    """Test helper: clear all registered pools."""
    _POOLS.clear()
