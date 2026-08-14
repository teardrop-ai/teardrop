from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from shared.db_pool import PgConnection, PgPool, _prepare_query, create_pool, translate_sql


def test_translate_sql_replaces_indexed_placeholders() -> None:
    assert translate_sql("SELECT $1, $2::text[]") == "SELECT %s, %s::text[]"


def test_prepare_query_reorders_and_repeats_arguments() -> None:
    query, params = _prepare_query("SELECT $2, $1, $2", ("first", "second"))

    assert query == "SELECT %s, %s, %s"
    assert params == ("second", "first", "second")


def test_prepare_query_rejects_missing_argument() -> None:
    with pytest.raises(ValueError, match=r"\$2"):
        _prepare_query("SELECT $2", ("first",))


def test_psycopg_errors_are_the_public_exception_types() -> None:
    assert issubclass(UniqueViolation, Exception)
    assert issubclass(CheckViolation, Exception)


async def test_connection_fetch_methods_and_status_message() -> None:
    cursor = MagicMock(statusmessage="DELETE 2")
    cursor.fetchall = AsyncMock(return_value=[{"id": "one"}, {"id": "two"}])
    cursor.fetchone = AsyncMock(return_value={"count": 2})
    raw_connection = MagicMock()
    raw_connection.execute = AsyncMock(return_value=cursor)
    connection = PgConnection(raw_connection)

    assert await connection.execute("DELETE FROM items WHERE id = $1", "one") == "DELETE 2"
    assert await connection.fetch("SELECT id FROM items") == [{"id": "one"}, {"id": "two"}]
    assert await connection.fetchrow("SELECT COUNT(*) AS count FROM items") == {"count": 2}
    assert await connection.fetchval("SELECT COUNT(*) AS count FROM items") == 2
    raw_connection.execute.assert_any_await("DELETE FROM items WHERE id = %s", ("one",))
    raw_connection.execute.assert_any_await("SELECT id FROM items")


def test_connection_delegates_transaction_context() -> None:
    transaction = MagicMock()
    raw_connection = MagicMock()
    raw_connection.transaction.return_value = transaction

    assert PgConnection(raw_connection).transaction(force_rollback=True) is transaction
    raw_connection.transaction.assert_called_once_with(force_rollback=True)


async def test_pool_acquire_wraps_connection() -> None:
    raw_connection = MagicMock()
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=raw_connection)
    connection_context.__aexit__ = AsyncMock(return_value=False)
    raw_pool = MagicMock()
    raw_pool.connection.return_value = connection_context

    async with PgPool(raw_pool).acquire() as connection:
        assert isinstance(connection, PgConnection)

    connection_context.__aexit__.assert_awaited_once()


async def test_create_pool_uses_application_connection_defaults() -> None:
    raw_pool = MagicMock()
    raw_pool.open = AsyncMock()

    with patch("shared.db_pool.AsyncConnectionPool", return_value=raw_pool) as pool_factory:
        pool = await create_pool(
            "postgresql://example",
            min_size=2,
            max_size=6,
            command_timeout=12.5,
            open_timeout=45.0,
        )

    assert isinstance(pool, PgPool)
    kwargs = pool_factory.call_args.kwargs
    assert kwargs["conninfo"] == "postgresql://example"
    assert kwargs["min_size"] == 2
    assert kwargs["max_size"] == 6
    assert kwargs["open"] is False
    assert kwargs["kwargs"]["autocommit"] is True
    assert kwargs["kwargs"]["row_factory"] is not None
    # statement_timeout must NOT go in the startup packet: Neon's PgBouncer
    # pooler rejects it. It is set per session via the configure callback.
    assert "options" not in kwargs["kwargs"]
    raw_pool.open.assert_awaited_once_with(wait=True, timeout=45.0)

    connection = MagicMock()
    connection.execute = AsyncMock()
    await kwargs["configure"](connection)
    connection.execute.assert_awaited_once_with("SET statement_timeout = 12500")


async def test_create_pool_configure_wraps_user_callback() -> None:
    raw_pool = MagicMock()
    raw_pool.open = AsyncMock()
    user_configure = AsyncMock()

    with patch("shared.db_pool.AsyncConnectionPool", return_value=raw_pool) as pool_factory:
        await create_pool("postgresql://example", configure=user_configure)

    connection = MagicMock()
    connection.execute = AsyncMock()
    await pool_factory.call_args.kwargs["configure"](connection)
    connection.execute.assert_awaited_once_with("SET statement_timeout = 30000")
    user_configure.assert_awaited_once_with(connection)
