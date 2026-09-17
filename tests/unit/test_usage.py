# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

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


@pytest.mark.anyio
class TestMachineFunnel:
    async def test_records_idempotent_mcp_billing_outcome(self):
        from teardrop.usage import record_mcp_call_event

        pool = _pool()
        with patch.object(usage_module, "_pool", pool):
            await record_mcp_call_event(
                "call-1",
                "",
                "0xabc",
                "platform/get_token_price",
                "x402",
                2_000,
                "settled",
                "0xtx",
            )

        sql, *values = pool.execute.await_args.args
        assert "INSERT INTO mcp_call_events" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert values == ["call-1", "", "0xabc", "platform/get_token_price", "x402", 2_000, "settled", "0xtx"]

    async def test_record_is_noop_without_pool_and_swallows_db_errors(self):
        from teardrop.usage import record_mcp_call_event

        with patch.object(usage_module, "_pool", None):
            await record_mcp_call_event("call-1", "", "", "tool", "x402", 1, "failed")

        pool = _pool()
        pool.execute.side_effect = RuntimeError("DB unavailable")
        with patch.object(usage_module, "_pool", pool):
            await record_mcp_call_event("call-1", "", "", "tool", "x402", 1, "failed")

    async def test_derives_conversion_failure_and_repeat_gate(self):
        from teardrop.usage import get_machine_funnel

        pool = _pool()
        pool.fetchrow.return_value = {
            "machine_orgs_provisioned": 3,
            "siwe_orgs_provisioned": 1,
            "x402_orgs_provisioned": 2,
            "settlement_attempts": 5,
            "settled_calls": 4,
            "failed_calls": 1,
            "settled_revenue_usdc": 8_000,
            "anonymous_settled_calls": 3,
            "org_bound_settled_calls": 1,
            "unique_anonymous_payers": 2,
            "converted_payers": 1,
            "repeat_payers": 1,
        }
        with patch.object(usage_module, "_pool", pool):
            report = await get_machine_funnel(14)

        assert report.settlement_failure_rate == 0.2
        assert report.wallet_conversion_rate == 0.5
        assert report.repeat_payer_rate == 0.5
        assert report.repeat_payer_gate is True
        sql = pool.fetchrow.await_args.args[0]
        assert "org_provisioning_events" in sql
        assert "e.created_at >= p.first_call_at" in sql

    async def test_empty_window_has_undefined_rates(self):
        from teardrop.usage import get_machine_funnel

        pool = _pool()
        pool.fetchrow.return_value = dict.fromkeys(
            (
                "machine_orgs_provisioned",
                "siwe_orgs_provisioned",
                "x402_orgs_provisioned",
                "settlement_attempts",
                "settled_calls",
                "failed_calls",
                "settled_revenue_usdc",
                "anonymous_settled_calls",
                "org_bound_settled_calls",
                "unique_anonymous_payers",
                "converted_payers",
                "repeat_payers",
            ),
            0,
        )
        with patch.object(usage_module, "_pool", pool):
            report = await get_machine_funnel(7)

        assert report.settlement_failure_rate is None
        assert report.wallet_conversion_rate is None
        assert report.repeat_payer_rate is None
        assert report.repeat_payer_gate is False


@pytest.mark.anyio
class TestDiscoveryFunnel:
    async def test_aggregates_stage_counts_and_conversion(self):
        from teardrop.usage import get_discovery_funnel

        pool = _pool()

        def _row(day, challenges, settled):
            return {
                "day": datetime(2026, 9, day, tzinfo=timezone.utc),
                "agent_card_hits": 10,
                "x402_discovery_hits": 8,
                "mcp_server_card_hits": 6,
                "catalog_hits": 5,
                "quote_hits": 4,
                "tools_list_hits": 3,
                "mcp_402_challenges": challenges,
                "settled_calls": settled,
            }

        pool.fetch = AsyncMock(return_value=[_row(16, 1, 0), _row(17, 1, 1)])
        with patch.object(usage_module, "_pool", pool):
            report = await get_discovery_funnel(14)

        assert report.window_days == 14
        assert report.agent_card_hits == 20
        assert report.x402_discovery_hits == 16
        assert report.mcp_server_card_hits == 12
        assert report.catalog_hits == 10
        assert report.quote_hits == 8
        assert report.tools_list_hits == 6
        assert report.mcp_402_challenges == 2
        assert report.settled_calls == 1
        assert report.challenge_to_settle_rate == 0.5
        assert [day.date for day in report.series] == ["2026-09-16", "2026-09-17"]
        assert report.series[0].settled_calls == 0
        assert report.series[1].settled_calls == 1
        sql = pool.fetch.await_args.args[0]
        assert "discovery_stage_counts" in sql
        assert "mcp_call_events" in sql
        assert "GROUP BY" in sql

    async def test_empty_window_has_empty_series_and_undefined_conversion(self):
        from teardrop.usage import get_discovery_funnel

        pool = _pool()
        pool.fetch = AsyncMock(return_value=[])
        with patch.object(usage_module, "_pool", pool):
            report = await get_discovery_funnel(7)

        assert report.challenge_to_settle_rate is None
        assert report.series == []
        assert report.mcp_402_challenges == 0

    async def test_rejects_unbounded_window(self):
        from teardrop.usage import get_discovery_funnel

        with pytest.raises(ValueError, match="days must be between 1 and 90"):
            await get_discovery_funnel(0)


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
            await record_tool_call_events("run-1", "org-1", entries, source="schedule")

        pool.executemany.assert_called_once()
        sql, rows = pool.executemany.call_args.args
        assert "tool_call_events" in sql
        assert len(rows) == 2
        assert rows[0][1] == "run-1"  # run_id
        assert rows[0][2] == "org-1"  # org_id
        assert rows[0][3] == "get_datetime"  # tool_name
        assert rows[0][9] == "schedule"  # source
        assert rows[1][4] is False  # success
        assert rows[1][5] == "timeout"  # error_class
        assert rows[0][-2] == usage_module.TOOL_CALL_EVENT_SCHEMA_VERSION

    async def test_unknown_source_defaults_to_api(self):
        from teardrop.usage import record_tool_call_events

        pool = _pool()
        pool.executemany = AsyncMock(return_value=None)
        with patch.object(usage_module, "_pool", pool):
            await record_tool_call_events("run-1", "org-1", [{"tool_name": "x"}], source="unknown")

        rows = pool.executemany.call_args.args[1]
        assert rows[0][9] == "api"

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
        pool.fetchrow = AsyncMock(
            return_value={
                "total_runs": 5,
                "total_tokens_in": 1000,
                "total_tokens_out": 500,
                "total_tool_calls": 10,
                "total_duration_ms": 3000,
            }
        )
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
        pool.fetchrow = AsyncMock(
            return_value={
                "total_runs": 0,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_tool_calls": 0,
                "total_duration_ms": 0,
            }
        )
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
        pool.fetchrow = AsyncMock(
            return_value={
                "total_runs": 3,
                "total_tokens_in": 600,
                "total_tokens_out": 300,
                "total_tool_calls": 5,
                "total_duration_ms": 1500,
            }
        )
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
