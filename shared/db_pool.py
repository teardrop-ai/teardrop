# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared Postgres pool facade and registry."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

__all__ = ["CheckViolation", "PgConnection", "PgPool", "UniqueViolation", "create_pool", "translate_sql"]

_PLACEHOLDER_RE = re.compile(r"\$([1-9][0-9]*)")

Row = Mapping[str, Any]


def translate_sql(query: str) -> str:
    """Translate asyncpg positional placeholders to psycopg placeholders."""
    return _PLACEHOLDER_RE.sub("%s", query)


def _prepare_query(query: str, args: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
    """Translate placeholders and reorder arguments for repeated or sparse indexes."""
    bound_args: list[Any] = []

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        if index >= len(args):
            raise ValueError(f"SQL placeholder ${index + 1} has no bound argument")
        bound_args.append(args[index])
        return "%s"

    translated = _PLACEHOLDER_RE.sub(replace, query)
    return translated, tuple(bound_args)


class PgConnection:
    """Expose the asyncpg methods used by Teardrop over a psycopg connection."""

    def __init__(self, connection: AsyncConnection[Row]) -> None:
        self._connection = connection

    async def _execute_cursor(self, query: str, args: tuple[Any, ...]):
        translated, params = _prepare_query(query, args)
        if not params:
            return await self._connection.execute(translated)
        return await self._connection.execute(translated, params)

    async def execute(self, query: str, *args: Any) -> str:
        cursor = await self._execute_cursor(query, args)
        return cursor.statusmessage or ""

    async def fetch(self, query: str, *args: Any) -> list[Row]:
        cursor = await self._execute_cursor(query, args)
        return await cursor.fetchall()

    async def fetchrow(self, query: str, *args: Any) -> Row | None:
        cursor = await self._execute_cursor(query, args)
        return await cursor.fetchone()

    async def fetchval(self, query: str, *args: Any) -> Any:
        row = await self.fetchrow(query, *args)
        if row is None:
            return None
        return next(iter(row.values()))

    def transaction(self, *args: Any, **kwargs: Any):
        return self._connection.transaction(*args, **kwargs)


class PgPool:
    """Application pool facade preserving Teardrop's existing async DB API."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Row]]) -> None:
        self._pool = pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[PgConnection]:
        async with self._pool.connection() as connection:
            yield PgConnection(connection)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.acquire() as connection:
            return await connection.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[Row]:
        async with self.acquire() as connection:
            return await connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Row | None:
        async with self.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self.acquire() as connection:
            return await connection.fetchval(query, *args)

    async def close(self) -> None:
        await self._pool.close()


async def create_pool(
    conninfo: str,
    *,
    min_size: int = 4,
    max_size: int = 4,
    command_timeout: float = 30.0,
    configure: Callable[[AsyncConnection[Row]], Awaitable[None]] | None = None,
    name: str = "teardrop-application",
) -> PgPool:
    """Create and open the application pool with consistent connection defaults."""
    statement_timeout_ms = max(1, round(command_timeout * 1000))
    raw_pool = AsyncConnectionPool(
        conninfo=conninfo,
        min_size=min_size,
        max_size=max_size,
        open=False,
        configure=configure,
        check=AsyncConnectionPool.check_connection,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "options": f"-c statement_timeout={statement_timeout_ms}",
        },
        name=name,
    )
    await raw_pool.open(wait=True)
    return PgPool(raw_pool)


_POOLS: dict[str, PgPool] = {}


def bind_pool(scope: str, pool: PgPool) -> PgPool:
    """Register and return a pool for the given scope."""
    _POOLS[scope] = pool
    return pool


def unbind_pool(scope: str) -> None:
    """Remove a pool binding for the given scope."""
    _POOLS.pop(scope, None)


def require_pool(scope: str, local_pool: PgPool | None, error_message: str) -> PgPool:
    """Return the module's local pool and keep the shared registry in sync."""
    if local_pool is None:
        raise RuntimeError(error_message)
    if _POOLS.get(scope) is not local_pool:
        _POOLS[scope] = local_pool
    return local_pool


def get_bound_pool(scope: str) -> PgPool | None:
    """Return a bound pool for a scope when present."""
    return _POOLS.get(scope)


def clear_all_bound_pools() -> None:
    """Test helper: clear all registered pools."""
    _POOLS.clear()
