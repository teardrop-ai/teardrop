from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from billing import BillingResult
from scheduling.models import ScheduledRun, ScheduledRunResult
from teardrop.agent_runtime import AgentRunOnceResult


def _schedule(callback_url: str | None = None) -> ScheduledRun:
    now = datetime.now(timezone.utc)
    return ScheduledRun(
        id="sched-1",
        org_id="org-1",
        user_id="user-1",
        name="Daily",
        prompt="Summarize",
        interval_seconds=3600,
        callback_url=callback_url,
        next_run_at=now,
        created_at=now,
        updated_at=now,
    )


def _stored_result(status: str = "completed", error: str = "") -> ScheduledRunResult:
    now = datetime.now(timezone.utc)
    return ScheduledRunResult(
        id="result-1",
        schedule_id="sched-1",
        org_id="org-1",
        run_id="run-1",
        status=status,
        output_text="done",
        cost_usdc=123,
        error=error,
        created_at=now,
    )


@pytest.mark.anyio
async def test_execute_scheduled_run_skips_when_credit_unverified(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    monkeypatch.setattr("scheduling.runner.get_settings", lambda: test_settings)
    monkeypatch.setattr("scheduling.runner.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.get_current_pricing", AsyncMock(return_value=SimpleNamespace(run_price_usdc=1000)))
    monkeypatch.setattr(
        "scheduling.runner.verify_credit",
        AsyncMock(return_value=BillingResult(verified=False, error="Insufficient credit")),
    )
    record_mock = AsyncMock(return_value=_stored_result(status="skipped", error="Insufficient credit"))
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", record_mock)
    mark_skipped = AsyncMock(return_value=None)
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_skipped", mark_skipped)
    monkeypatch.setattr("scheduling.runner.run_agent_once", AsyncMock())

    from scheduling.runner import execute_scheduled_run

    result = await execute_scheduled_run(_schedule())

    assert result.status == "skipped"
    mark_skipped.assert_awaited_once()


@pytest.mark.anyio
async def test_execute_scheduled_run_blocks_ssrf_callback_without_failing_run(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    monkeypatch.setattr("scheduling.runner.get_settings", lambda: test_settings)
    monkeypatch.setattr("scheduling.runner.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.get_current_pricing", AsyncMock(return_value=SimpleNamespace(run_price_usdc=1000)))
    monkeypatch.setattr(
        "scheduling.runner.verify_credit",
        AsyncMock(return_value=BillingResult(verified=True, billing_method="credit")),
    )
    run_once = AsyncMock(
        return_value=AgentRunOnceResult(
            task_state="completed",
            response_state="completed",
            output_text="done",
            duration_ms=25,
            usage_event=SimpleNamespace(cost_usdc=123),
            usage_data={},
            llm_config=None,
            marketplace_stats_billable=False,
        )
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", run_once)
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", AsyncMock(return_value=_stored_result()))
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_succeeded", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.async_validate_url", AsyncMock(return_value="Blocked IP address"))
    httpx_client = AsyncMock()
    monkeypatch.setattr("scheduling.runner.httpx.AsyncClient", httpx_client)

    from scheduling.runner import execute_scheduled_run

    result = await execute_scheduled_run(_schedule(callback_url="https://169.254.169.254/hook"))

    assert result.status == "completed"
    httpx_client.assert_not_called()
    assert run_once.await_args.kwargs["source"] == "schedule"


@pytest.mark.anyio
async def test_execute_scheduled_run_marks_failure(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    monkeypatch.setattr("scheduling.runner.get_settings", lambda: test_settings)
    monkeypatch.setattr("scheduling.runner.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.get_current_pricing", AsyncMock(return_value=SimpleNamespace(run_price_usdc=1000)))
    monkeypatch.setattr(
        "scheduling.runner.verify_credit",
        AsyncMock(return_value=BillingResult(verified=True, billing_method="credit")),
    )
    monkeypatch.setattr(
        "scheduling.runner.run_agent_once",
        AsyncMock(
            return_value=AgentRunOnceResult(
                task_state="failed",
                response_state="failed",
                output_text="Task failed.",
                duration_ms=25,
                usage_event=SimpleNamespace(cost_usdc=0),
                usage_data={},
                llm_config=None,
                marketplace_stats_billable=False,
            )
        ),
    )
    monkeypatch.setattr(
        "scheduling.runner.record_scheduled_run_result",
        AsyncMock(return_value=_stored_result(status="failed", error="Task failed.")),
    )
    mark_failed = AsyncMock(return_value=None)
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_failed", mark_failed)

    from scheduling.runner import execute_scheduled_run

    result = await execute_scheduled_run(_schedule())

    assert result.status == "failed"
    mark_failed.assert_awaited_once()


@pytest.mark.anyio
async def test_execute_event_run_uses_caller_run_id_and_records(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    monkeypatch.setattr("scheduling.runner.get_settings", lambda: test_settings)
    monkeypatch.setattr("scheduling.runner.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.get_current_pricing", AsyncMock(return_value=SimpleNamespace(run_price_usdc=1000)))
    monkeypatch.setattr(
        "scheduling.runner.verify_credit",
        AsyncMock(return_value=BillingResult(verified=True, billing_method="credit")),
    )
    run_once = AsyncMock(
        return_value=AgentRunOnceResult(
            task_state="completed",
            response_state="completed",
            output_text="done",
            duration_ms=25,
            usage_event=SimpleNamespace(cost_usdc=123),
            usage_data={},
            llm_config=None,
            marketplace_stats_billable=False,
        )
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", run_once)
    record_mock = AsyncMock(return_value=_stored_result())
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", record_mock)
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_succeeded", AsyncMock(return_value=None))

    from scheduling.runner import execute_event_run

    result = await execute_event_run(_schedule(), prompt="rendered prompt", run_id="evt-run-1")

    assert result.status == "completed"
    # The caller-supplied run_id (idempotency anchor) must be used verbatim.
    assert run_once.await_args.kwargs["run_id"] == "evt-run-1"
    assert run_once.await_args.kwargs["user_message"] == "rendered prompt"
    assert run_once.await_args.kwargs["user_role"] == "event"
    assert run_once.await_args.kwargs["source"] == "trigger"
    assert record_mock.await_args.kwargs["run_id"] == "evt-run-1"
