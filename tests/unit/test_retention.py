"""Unit tests for disposable-data retention sweeps."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import teardrop.retention as retention_module


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "checkpoint_ttl_days": 45,
        "scheduled_run_results_ttl_days": 30,
        "org_tool_execution_events_ttl_days": 90,
        "telemetry_run_starts_ttl_days": 120,
        "retention_sweep_batch_size": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _checkpoint_pool(thread_batches: list[list[dict[str, str]]]) -> tuple[MagicMock, MagicMock]:
    connection = MagicMock()
    connection.fetch = AsyncMock(side_effect=thread_batches)
    connection.execute = AsyncMock()

    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    connection.transaction = MagicMock(return_value=transaction)

    acquire_context = MagicMock()
    acquire_context.__aenter__ = AsyncMock(return_value=connection)
    acquire_context.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_context)
    pool.fetchval = AsyncMock()
    pool.execute = AsyncMock()
    return pool, connection


@pytest.mark.anyio
class TestRetentionSweep:
    async def test_sweeps_only_disposable_records_in_batches(self):
        pool, connection = _checkpoint_pool(
            [
                [{"thread_id": "user-1:thread-1"}, {"thread_id": "user-2:thread-2"}],
                [],
            ]
        )
        pool.fetchval = AsyncMock(side_effect=[2, 0, 1, 1, 1])

        with patch.object(retention_module, "_pool", pool):
            result = await retention_module.retention_sweep_once(_settings())

        assert result.checkpoint_threads == 2
        assert result.scheduled_run_results == 2
        assert result.org_tool_execution_events == 1
        assert result.telemetry_run_starts == 1
        assert result.expired_siwe_login_sessions == 1
        assert result.total_deleted == 7

        checkpoint_delete_sql = [call.args[0] for call in connection.execute.await_args_list]
        assert checkpoint_delete_sql == [
            "DELETE FROM checkpoint_writes WHERE thread_id = ANY($1::TEXT[])",
            "DELETE FROM checkpoint_blobs WHERE thread_id = ANY($1::TEXT[])",
            "DELETE FROM checkpoints WHERE thread_id = ANY($1::TEXT[])",
            "DELETE FROM checkpoint_thread_activity WHERE thread_id = ANY($1::TEXT[])",
        ]

        cleanup_sql = "\n".join(
            [connection.fetch.await_args.args[0], *[call.args[0] for call in pool.fetchval.await_args_list]]
        ).lower()
        for protected_table in (
            "a2a_inbound_events",
            "a2a_delegation_events",
            "usage_events",
            "tool_call_events",
            "run_decisions",
            "org_credit_ledger",
            "pending_settlements",
            "stripe_webhook_events",
        ):
            assert protected_table not in cleanup_sql
        assert "telemetry_run_starts" in cleanup_sql

    async def test_zero_ttl_skips_configurable_cleanup_but_removes_expired_siwe_sessions(self):
        pool, connection = _checkpoint_pool([])
        pool.fetchval = AsyncMock(return_value=0)

        with patch.object(retention_module, "_pool", pool):
            result = await retention_module.retention_sweep_once(
                _settings(
                    checkpoint_ttl_days=0,
                    scheduled_run_results_ttl_days=0,
                    org_tool_execution_events_ttl_days=0,
                    telemetry_run_starts_ttl_days=0,
                )
            )

        assert result.total_deleted == 0
        connection.fetch.assert_not_awaited()
        pool.fetchval.assert_awaited_once()
        assert "siwe_login_sessions" in pool.fetchval.await_args.args[0]


@pytest.mark.anyio
class TestCheckpointActivity:
    async def test_touch_upserts_activity_without_logging_the_thread_id(self):
        pool, _connection = _checkpoint_pool([])

        with patch.object(retention_module, "_pool", pool):
            await retention_module.touch_checkpoint_thread("user-1:thread-1")

        sql, thread_id = pool.execute.await_args.args
        assert "ON CONFLICT (thread_id)" in sql
        assert thread_id == "user-1:thread-1"

    async def test_touch_is_noop_before_retention_initialization(self):
        with patch.object(retention_module, "_pool", None):
            await retention_module.touch_checkpoint_thread("user-1:thread-1")


@pytest.mark.anyio
class TestRetentionWorker:
    async def test_loop_uses_monitored_periodic_runner(self):
        from teardrop import _background_tasks

        run_periodic = AsyncMock()
        worker_settings = SimpleNamespace(retention_sweep_interval_seconds=1800)
        with (
            patch.object(_background_tasks, "settings", worker_settings),
            patch.object(_background_tasks, "_run_periodic", run_periodic),
        ):
            await _background_tasks._retention_sweep_loop()

        assert run_periodic.await_args.args[:3] == (
            "Retention sweep",
            _background_tasks._retention_sweep_iter,
            1800,
        )
        assert run_periodic.await_args.kwargs["monitor_slug"] == "retention-sweep"
