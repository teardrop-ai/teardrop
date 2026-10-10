# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for model_catalogue.py — OpenRouter catalogue parsing, storage and lookups."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from teardrop import model_catalogue as mc
from teardrop.benchmarks import _DEFAULT_MODEL_SPECS, get_model_context_specs


def _raw(or_id: str, **overrides: Any) -> dict[str, Any]:
    entry = {
        "id": or_id,
        "canonical_slug": or_id,
        "name": or_id.title(),
        "context_length": 1_048_576,
        "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
        "supported_parameters": ["tools", "temperature"],
        "expiration_date": None,
        "alias_target": None,
    }
    entry.update(overrides)
    return entry


class _FakeConn:
    def __init__(self, existing: list[dict[str, Any]], lock: bool = True) -> None:
        self.existing = existing
        self.lock = lock
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, query: str, *args: Any) -> Any:
        return self.lock

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self.existing

    async def executemany(self, query: str, rows) -> None:
        self.executemany_calls.append((query, list(rows)))

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 2"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class TestParseModel:
    def test_parses_alias_prices_and_tools(self):
        parsed = mc.parse_model(
            _raw(
                "~google/gemini-flash-latest",
                alias_target={"name": "Gemini", "slug": "google/gemini-3.8-flash"},
                expiration_date="2026-12-31",
            )
        )
        assert parsed is not None
        assert parsed["alias_target"] == "google/gemini-3.8-flash"
        assert parsed["prompt_price_usd"] == Decimal("0.0000003")
        assert parsed["supports_tools"] is True
        assert parsed["expiration_date"] == date(2026, 12, 31)
        assert parsed["context_length"] == 1_048_576

    def test_tolerates_missing_and_malformed_fields(self):
        parsed = mc.parse_model(
            {"id": "x/y", "pricing": {"prompt": "n/a"}, "supported_parameters": None, "expiration_date": "soon"}
        )
        assert parsed is not None
        assert parsed["prompt_price_usd"] is None
        assert parsed["supports_tools"] is False
        assert parsed["expiration_date"] is None
        assert parsed["alias_target"] is None

    def test_rejects_entry_without_id(self):
        assert mc.parse_model({"name": "nameless"}) is None


class TestStoreModels:
    @pytest.mark.asyncio
    async def test_logs_alias_retarget_and_marks_removals(self):
        conn = _FakeConn(
            existing=[
                {"or_id": "~a/latest", "alias_target": "a/v1"},
                {"or_id": "a/v1", "alias_target": None},
            ]
        )
        models = [
            mc.parse_model(_raw("~a/latest", alias_target={"slug": "a/v2"})),
            mc.parse_model(_raw("a/v2")),
        ]
        stats = await mc.store_models(_FakePool(conn), models)

        assert stats == {"upserted": 2, "alias_changes": 1, "removed": 2}
        alias_inserts = [rows for q, rows in conn.executemany_calls if "model_alias_events" in q]
        assert alias_inserts == [[("~a/latest", "a/v1", "a/v2")]]
        assert conn.execute_calls and "removed_at = NOW()" in conn.execute_calls[0][0]

    @pytest.mark.asyncio
    async def test_skips_removals_when_feed_shrinks(self):
        conn = _FakeConn(existing=[{"or_id": f"m/{i}", "alias_target": None} for i in range(10)])
        stats = await mc.store_models(_FakePool(conn), [mc.parse_model(_raw("m/0"))])
        assert stats["removed"] == 0
        assert conn.execute_calls == []

    @pytest.mark.asyncio
    async def test_noop_when_lock_held_elsewhere(self):
        conn = _FakeConn(existing=[], lock=False)
        stats = await mc.store_models(_FakePool(conn), [mc.parse_model(_raw("m/0"))])
        assert stats["upserted"] == 0
        assert conn.executemany_calls == []


class TestSyncOnce:
    @pytest.mark.asyncio
    async def test_skips_fetch_when_recently_synced(self):
        with (
            patch.object(mc, "_pool", object()),
            patch.object(mc, "_is_fresh", new_callable=AsyncMock, return_value=True),
            patch.object(mc, "fetch_openrouter_models", new_callable=AsyncMock) as fetch,
            patch.object(mc, "load_snapshot", new_callable=AsyncMock) as load,
        ):
            assert await mc.sync_model_catalogue_once() is None
            fetch.assert_not_awaited()
            load.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetches_and_stores_when_stale(self):
        models = [mc.parse_model(_raw("m/0"))]
        with (
            patch.object(mc, "_pool", object()),
            patch.object(mc, "_is_fresh", new_callable=AsyncMock, return_value=False),
            patch.object(mc, "fetch_openrouter_models", new_callable=AsyncMock, return_value=models),
            patch.object(mc, "store_models", new_callable=AsyncMock, return_value={"upserted": 1}) as store,
            patch.object(mc, "load_snapshot", new_callable=AsyncMock),
        ):
            assert await mc.sync_model_catalogue_once() == {"upserted": 1}
            store.assert_awaited_once()


class TestContextSpecsFallback:
    def test_uses_synced_openrouter_entry_for_uncatalogued_model(self):
        snapshot = {"~a/latest": {"context_length": 400_000, "supports_tools": False}}
        with patch.object(mc, "_snapshot", snapshot):
            specs = get_model_context_specs("openrouter", "~a/latest")
        assert specs["context_window"] == 400_000
        assert specs["supports_tools"] is False
        assert specs["knowledge_cutoff"] == _DEFAULT_MODEL_SPECS["knowledge_cutoff"]

    def test_ignores_snapshot_for_non_openrouter_providers(self):
        with patch.object(mc, "_snapshot", {"m": {"context_length": 5}}):
            assert get_model_context_specs("google", "m") == _DEFAULT_MODEL_SPECS
