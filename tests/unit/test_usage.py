"""Unit tests for usage.py — DB functions mocked via pool MagicMock."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import usage as usage_module
from usage import UsageEvent, UsageSummary


# ─── Pool mock helper ─────────────────────────────────────────────────────────


def _pool():
    pool = MagicMock()
    pool.execute = AsyncMock(return_value=None)
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


# ─── record_usage_event ───────────────────────────────────────────────────────


@pytest.mark.anyio
class TestRecordUsageEvent:
    async def test_inserts_event(self):
        from usage import record_usage_event

        pool = _pool()
        event = UsageEvent(
            user_id="user-1",
            org_id="org-1",
            thread_id="thread-1",
            run_id="run-1",
            tokens_in=100,
            tokens_out=50,
            tool_calls=2,
            tool_names=["get_datetime", "calculate"],
            duration_ms=300,
        )
        with patch.object(usage_module, "_pool", pool):
            await record_usage_event(event)
        pool.execute.assert_called_once()
        call_args = pool.execute.call_args.args
        assert "run-1" in call_args
        assert 100 in call_args

    async def test_db_error_is_swallowed(self):
        from usage import record_usage_event

        pool = _pool()
        pool.execute = AsyncMock(side_effect=Exception("DB gone"))
        event = UsageEvent(
            user_id="u", org_id="o", thread_id="t", run_id="r"
        )
        with patch.object(usage_module, "_pool", pool):
            # Must not raise
            await record_usage_event(event)


# ─── get_usage_by_user / get_usage_by_org ────────────────────────────────────


@pytest.mark.anyio
class TestGetUsage:
    async def test_returns_summary_with_values(self):
        from usage import get_usage_by_user

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=(5, 1000, 500, 10, 3000))
        with patch.object(usage_module, "_pool", pool):
            summary = await get_usage_by_user("user-1")
        assert isinstance(summary, UsageSummary)
        assert summary.total_runs == 5
        assert summary.total_tokens_in == 1000

    async def test_returns_zero_summary_when_no_row(self):
        from usage import get_usage_by_user

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(usage_module, "_pool", pool):
            summary = await get_usage_by_user("user-nobody")
        assert summary.total_runs == 0

    async def test_date_range_params_included(self):
        from usage import get_usage_by_user

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=(0, 0, 0, 0, 0))
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 12, 31, tzinfo=timezone.utc)
        with patch.object(usage_module, "_pool", pool):
            await get_usage_by_user("user-1", start=start, end=end)
        call_args = pool.fetchrow.call_args.args
        assert start in call_args
        assert end in call_args

    async def test_get_usage_by_org(self):
        from usage import get_usage_by_org

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=(3, 600, 300, 5, 1500))
        with patch.object(usage_module, "_pool", pool):
            summary = await get_usage_by_org("org-1")
        assert summary.total_runs == 3


# ─── init / close helpers ─────────────────────────────────────────────────────


@pytest.mark.anyio
class TestInitAndClose:
    async def test_close_usage_db_clears_pool(self):
        from usage import close_usage_db

        with patch.object(usage_module, "_pool", MagicMock()):
            await close_usage_db()
        assert usage_module._pool is None

    def test_get_pool_raises_when_uninitialised(self):
        from usage import _get_pool

        with patch.object(usage_module, "_pool", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                _get_pool()
