# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared Postgres pool facade and registry."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

__all__ = ["CheckViolation", "PgConnection", "PgPool", "UniqueViolation", "create_pool", "translate_sql"]

_PLACEHOLDER_RE = re.compile(r"\$([1-9][0-9]*)")

Row = Mapping[str, Any]
logger = logging.getLogger(__name__)


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

    async def executemany(self, query: str, args_seq: Iterable[Sequence[Any]]) -> None:
        """Execute the same query for a batch of argument rows.

        Placeholder translation/reordering is applied per row so repeated or
        sparse ``$N`` indexes behave the same as in ``execute``/``fetch``.
        """
        rows = [tuple(row) for row in args_seq]
        if not rows:
            return
        translated, _ = _prepare_query(query, rows[0])
        params = [_prepare_query(query, row)[1] for row in rows]
        async with self._connection.cursor() as cursor:
            await cursor.executemany(translated, params)

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

    async def executemany(self, query: str, args_seq: Iterable[Sequence[Any]]) -> None:
        async with self.acquire() as connection:
            await connection.executemany(query, args_seq)

    async def close(self) -> None:
        await self._pool.close()


async def create_pool(
    conninfo: str,
    *,
    min_size: int = 4,
    max_size: int = 4,
    command_timeout: float = 30.0,
    open_timeout: float = 60.0,
    configure: Callable[[AsyncConnection[Row]], Awaitable[None]] | None = None,
    name: str = "teardrop-application",
) -> PgPool:
    """Create and open the application pool with consistent connection defaults.

    ``statement_timeout`` is applied per session inside ``configure`` rather than
    via the startup packet's ``options`` parameter: Neon's PgBouncer pooler
    endpoint rejects arbitrary startup options ("unsupported startup parameter"),
    which prevented pool initialization entirely.
    """
    statement_timeout_ms = max(1, round(command_timeout * 1000))

    async def _configure(connection: AsyncConnection[Row]) -> None:
        await connection.execute(f"SET statement_timeout = {statement_timeout_ms}")
        if configure is not None:
            await configure(connection)

    raw_pool = AsyncConnectionPool(
        conninfo=conninfo,
        min_size=min_size,
        max_size=max_size,
        open=False,
        configure=_configure,
        check=AsyncConnectionPool.check_connection,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
        },
        name=name,
    )
    try:
        await raw_pool.open(wait=True, timeout=open_timeout)
    except Exception as exc:
        logger.error(
            "Failed to initialize Postgres pool '%s' (min_size=%d max_size=%d open_timeout=%.1fs): %s: %s",
            name,
            min_size,
            max_size,
            open_timeout,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        raise
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
