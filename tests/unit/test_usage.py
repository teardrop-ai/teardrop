"""Unit tests for usage.py — DB functions mocked via pool MagicMock."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import teardrop.usage as usage_module
from teardrop._meta import APP_VERSION
from teardrop.usage import UsageEvent, UsageSummary

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
        from teardrop.usage import record_usage_event

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
        assert call_args[-3] == "api"
        assert call_args[-2] == APP_VERSION

    def test_defaults_runner_version_to_application_version(self):
        event = UsageEvent(user_id="user-1", org_id="org-1", thread_id="thread-1", run_id="run-1")

        assert event.runner_version == APP_VERSION

    async def test_db_error_is_swallowed(self):
        from teardrop.usage import record_usage_event

        pool = _pool()
        pool.execute = AsyncMock(side_effect=Exception("DB gone"))
        event = UsageEvent(user_id="u", org_id="o", thread_id="t", run_id="r")
        with patch.object(usage_module, "_pool", pool):
            # Must not raise
            await record_usage_event(event)


@pytest.mark.anyio
class TestTelemetryRunStart:
    async def test_inserts_once_with_source(self):
        from teardrop.usage import record_telemetry_run_started

        pool = _pool()
        with patch.object(usage_module, "_pool", pool):
            await record_telemetry_run_started("run-1", "org-1", "trigger")

        sql, run_id, org_id, source = pool.execute.await_args.args
        assert "telemetry_run_starts" in sql
        assert "ON CONFLICT (run_id) DO NOTHING" in sql
        assert (run_id, org_id, source) == ("run-1", "org-1", "trigger")

    async def test_is_noop_before_database_initialization(self):
        from teardrop.usage import record_telemetry_run_started

        with patch.object(usage_module, "_pool", None):
            await record_telemetry_run_started("run-1", "org-1", "api")


@pytest.mark.anyio
class TestTelemetryCompleteness:
    async def test_returns_source_split_coverage(self):
        from teardrop.usage import get_telemetry_completeness

        pool = _pool()
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "source": "api",
                    "total_runs": 10,
                    "usage_event_runs": 8,
                    "tool_eligible_runs": 4,
                    "tool_event_runs": 3,
                    "decision_runs": 6,
                    "outcome_label_runs": 5,
                }
            ]
        )

        with patch.object(usage_module, "_pool", pool):
            result = await get_telemetry_completeness(7)

        assert result[0].source == "api"
        assert result[0].usage_event_coverage == 0.8
        assert result[0].tool_eligible_runs == 4
        assert result[0].tool_event_coverage == 0.75
        assert result[0].decision_coverage == 0.6
        assert result[0].outcome_label_coverage == 0.5
        assert "telemetry_run_starts" in pool.fetch.await_args.args[0]

    async def test_rejects_unbounded_window(self):
        from teardrop.usage import get_telemetry_completeness

        with pytest.raises(ValueError, match="days must be between 1 and 90"):
            await get_telemetry_completeness(0)


# ─── record_tool_call_events ──────────────────────────────────────────────────


@pytest.mark.anyio
class TestRecordToolCallEvents:
    async def test_inserts_one_row_per_entry(self):
        from teardrop.usage import record_tool_call_events

        pool = _pool()
        pool.executemany = AsyncMock(return_value=None)
        entries = [
            {
                "tool_name": "get_datetime",
                "success": True,
                "error_class": "",
                "elapsed_ms": 42,
                "billable": True,
                "args_hash": "abc123",
            },
            {
                "tool_name": "broken_tool",
                "success": False,
                "error_class": "timeout",
                "elapsed_ms": 500,
                "billable": False,
                "args_hash": "def456",
            },
        ]
        with patch.object(usage_module, "_pool", pool):
            await record_tool_call_events("run-1", "org-1", entries)

        pool.executemany.assert_called_once()
        sql, rows = pool.executemany.call_args.args
        assert "tool_call_events" in sql
        assert len(rows) == 2
        assert rows[0][1] == "run-1"  # run_id
        assert rows[0][2] == "org-1"  # org_id
        assert rows[0][3] == "get_datetime"  # tool_name
        assert rows[1][4] is False  # success
        assert rows[1][5] == "timeout"  # error_class
        assert rows[0][-2] == usage_module.TOOL_CALL_EVENT_SCHEMA_VERSION

    async def test_empty_entries_is_noop(self):
        from teardrop.usage import record_tool_call_events

        pool = _pool()
        pool.executemany = AsyncMock(return_value=None)
        with patch.object(usage_module, "_pool", pool):
            await record_tool_call_events("run-1", "org-1", [])

        pool.executemany.assert_not_called()

    async def test_db_error_is_swallowed(self):
        from teardrop.usage import record_tool_call_events

        pool = _pool()
        pool.executemany = AsyncMock(side_effect=Exception("DB gone"))
        with patch.object(usage_module, "_pool", pool):
            # Must not raise
            await record_tool_call_events("run-1", "org-1", [{"tool_name": "x"}])


# ─── get_usage_by_user / get_usage_by_org ────────────────────────────────────


@pytest.mark.anyio
class TestGetUsage:
    async def test_returns_summary_with_values(self):
        from teardrop.usage import get_usage_by_user

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=(5, 1000, 500, 10, 3000))
        with patch.object(usage_module, "_pool", pool):
            summary = await get_usage_by_user("user-1")
        assert isinstance(summary, UsageSummary)
        assert summary.total_runs == 5
        assert summary.total_tokens_in == 1000

    async def test_returns_zero_summary_when_no_row(self):
        from teardrop.usage import get_usage_by_user

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(usage_module, "_pool", pool):
            summary = await get_usage_by_user("user-nobody")
        assert summary.total_runs == 0

    async def test_date_range_params_included(self):
        from teardrop.usage import get_usage_by_user

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
        from teardrop.usage import get_usage_by_org

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=(3, 600, 300, 5, 1500))
        with patch.object(usage_module, "_pool", pool):
            summary = await get_usage_by_org("org-1")
        assert summary.total_runs == 3


# ─── init / close helpers ─────────────────────────────────────────────────────


@pytest.mark.anyio
class TestInitAndClose:
    async def test_close_usage_db_clears_pool(self):
        from teardrop.usage import close_usage_db

        with patch.object(usage_module, "_pool", MagicMock()):
            await close_usage_db()
        assert usage_module._pool is None

    def test_get_pool_raises_when_uninitialised(self):
        from teardrop.usage import _get_pool

        with patch.object(usage_module, "_pool", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                _get_pool()

    async def test_init_usage_db_sets_pool_and_creates_tables(self):
        from teardrop.usage import init_usage_db

        pool = MagicMock()
        pool.execute = AsyncMock()
        saved = usage_module._pool
        try:
            await init_usage_db(pool)
            assert usage_module._pool is pool
            assert pool.execute.call_count == 3  # CREATE TABLE + 2 CREATE INDEX
        finally:
            usage_module._pool = saved
