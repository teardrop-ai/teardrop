# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from billing import BillingResult
from scheduling.models import ScheduledRun, ScheduledRunResult
from teardrop.agent_runtime import AgentRunOnceResult


def _schedule(callback_url: str | None = None, callback_format: str = "json") -> ScheduledRun:
    now = datetime.now(timezone.utc)
    return ScheduledRun(
        id="sched-1",
        org_id="org-1",
        user_id="user-1",
        name="Daily",
        prompt="Summarize",
        interval_seconds=3600,
        callback_url=callback_url,
        callback_format=callback_format,
        next_run_at=now,
        created_at=now,
        updated_at=now,
    )


def _stored_result(status: str = "completed", error: str = "", output_text: str = "done") -> ScheduledRunResult:
    now = datetime.now(timezone.utc)
    return ScheduledRunResult(
        id="result-1",
        schedule_id="sched-1",
        org_id="org-1",
        run_id="run-1",
        status=status,
        output_text=output_text,
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
async def test_text_callback_posts_only_human_report(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = SimpleNamespace(status_code=200)
    monkeypatch.setattr("scheduling.runner.async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.httpx.AsyncClient", MagicMock(return_value=client))

    from scheduling.runner import _deliver_callback

    await _deliver_callback(
        "https://notify.example/hook",
        {"output_text": "## Entry Candidates\n- token-1", "run_id": "run-1"},
        "schedule-1",
        "text",
    )

    kwargs = client.post.await_args.kwargs
    assert kwargs["content"] == "## Entry Candidates\n- token-1"
    assert kwargs["headers"]["content-type"] == "text/plain; charset=utf-8"
    assert "json" not in kwargs


@pytest.mark.anyio
async def test_text_callback_strips_leading_prediction_json(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = SimpleNamespace(status_code=200)
    monkeypatch.setattr("scheduling.runner.async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.httpx.AsyncClient", MagicMock(return_value=client))

    from scheduling.runner import _deliver_callback

    await _deliver_callback(
        "https://notify.example/hook",
        {
            "output_text": '{"task_class":"entry_timing","schema_version":1}\n## Entry Candidates\n- token-1',
            "run_id": "run-1",
        },
        "schedule-1",
        "text",
    )

    kwargs = client.post.await_args.kwargs
    assert kwargs["content"] == "## Entry Candidates\n- token-1"


@pytest.mark.anyio
async def test_labeling_ingestion_finishes_before_scheduled_run_returns(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    test_settings.labeling_enabled = True
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
                task_state="completed",
                response_state="completed",
                output_text="done",
                duration_ms=25,
                usage_event=SimpleNamespace(cost_usdc=123),
                usage_data={},
                llm_config=None,
                marketplace_stats_billable=False,
            )
        ),
    )
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", AsyncMock(return_value=_stored_result()))
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_succeeded", AsyncMock(return_value=None))
    ingest = AsyncMock()
    monkeypatch.setattr("scheduling.runner._ingest_labeling_prediction", ingest)

    from scheduling.runner import execute_scheduled_run

    result = await execute_scheduled_run(_schedule())

    assert result.status == "completed"
    ingest.assert_awaited_once()


@pytest.mark.anyio
async def test_timeout_preserves_prediction_capture_for_labeling(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    test_settings.labeling_enabled = True
    monkeypatch.setattr("scheduling.runner.get_settings", lambda: test_settings)
    monkeypatch.setattr("scheduling.runner.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.get_current_pricing", AsyncMock(return_value=SimpleNamespace(run_price_usdc=1000)))
    monkeypatch.setattr(
        "scheduling.runner.verify_credit",
        AsyncMock(return_value=BillingResult(verified=True, billing_method="credit")),
    )
    run_result = AgentRunOnceResult(
        task_state="timeout",
        response_state="failed",
        output_text="partial report",
        duration_ms=25,
        usage_event=SimpleNamespace(cost_usdc=0),
        usage_data={"_tool_call_log": [{"tool_name": "record_predictions", "success": True}]},
        llm_config=None,
        marketplace_stats_billable=False,
        error="Task timed out.",
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", AsyncMock(return_value=run_result))
    record_mock = AsyncMock(return_value=_stored_result(status="timeout", error="Task timed out."))
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", record_mock)
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_failed", AsyncMock(return_value=None))
    ingest = AsyncMock()
    monkeypatch.setattr("scheduling.runner._ingest_labeling_prediction", ingest)

    from scheduling.runner import execute_scheduled_run

    result = await execute_scheduled_run(_schedule())

    assert result.status == "timeout"
    ingest.assert_awaited_once()
    assert record_mock.await_args.kwargs["output_text"] == "partial report"
    assert record_mock.await_args.kwargs["error"] == "Task timed out."


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


@pytest.mark.anyio
async def test_x_broadcast_posts_when_completed(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    test_settings.x_broadcast_enabled = True
    test_settings.x_broadcast_org_id = "org-1"
    test_settings.x_api_key = "k"
    test_settings.x_api_secret = "s"
    test_settings.x_access_token = "t"
    test_settings.x_access_token_secret = "ts"
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
            output_text='{"task_class":"market_scan"}\nMarket verdict: Aave USDC yield is stable at 4.2%.',
            duration_ms=25,
            usage_event=SimpleNamespace(cost_usdc=123),
            usage_data={},
            llm_config=None,
            marketplace_stats_billable=False,
        )
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", run_once)
    stored = _stored_result(output_text='{"task_class":"market_scan"}\nMarket verdict: Aave USDC yield is stable at 4.2%.')
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", AsyncMock(return_value=stored))
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_succeeded", AsyncMock(return_value=None))

    reserve_mock = AsyncMock(return_value=True)
    record_tweet_mock = AsyncMock()
    post_tweet_mock = AsyncMock(return_value="tweet-123456")
    monkeypatch.setattr("scheduling.runner.reserve_x_broadcast", reserve_mock)
    monkeypatch.setattr("scheduling.runner.record_x_broadcast_tweet", record_tweet_mock)
    monkeypatch.setattr("shared.x_client.post_tweet", post_tweet_mock)

    from scheduling.runner import execute_scheduled_run

    sched = _schedule(callback_format="x")
    result = await execute_scheduled_run(sched)

    assert result.status == "completed"
    reserve_mock.assert_awaited_once()
    post_tweet_mock.assert_awaited_once_with("Market verdict: Aave USDC yield is stable at 4.2%.")
    record_tweet_mock.assert_awaited_once_with(stored.run_id, "tweet-123456")


@pytest.mark.anyio
async def test_x_broadcast_rejects_url_content(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    test_settings.x_broadcast_enabled = True
    test_settings.x_broadcast_org_id = "org-1"
    test_settings.x_api_key = "k"
    test_settings.x_api_secret = "s"
    test_settings.x_access_token = "t"
    test_settings.x_access_token_secret = "ts"
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
            output_text="Check out https://scam.example for yields!",
            duration_ms=25,
            usage_event=SimpleNamespace(cost_usdc=123),
            usage_data={},
            llm_config=None,
            marketplace_stats_billable=False,
        )
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", run_once)
    stored = _stored_result(output_text="Check out https://scam.example for yields!")
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", AsyncMock(return_value=stored))
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_succeeded", AsyncMock(return_value=None))

    reserve_mock = AsyncMock(return_value=True)
    post_tweet_mock = AsyncMock()
    monkeypatch.setattr("scheduling.runner.reserve_x_broadcast", reserve_mock)
    monkeypatch.setattr("shared.x_client.post_tweet", post_tweet_mock)

    from scheduling.runner import execute_scheduled_run

    sched = _schedule(callback_format="x")
    result = await execute_scheduled_run(sched)

    assert result.status == "completed"
    reserve_mock.assert_not_awaited()
    post_tweet_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x_broadcast_skips_when_status_not_completed(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    test_settings.x_broadcast_enabled = True
    test_settings.x_broadcast_org_id = "org-1"
    test_settings.x_api_key = "k"
    test_settings.x_api_secret = "s"
    test_settings.x_access_token = "t"
    test_settings.x_access_token_secret = "ts"
    monkeypatch.setattr("scheduling.runner.get_settings", lambda: test_settings)
    monkeypatch.setattr("scheduling.runner.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("scheduling.runner.get_current_pricing", AsyncMock(return_value=SimpleNamespace(run_price_usdc=1000)))
    monkeypatch.setattr(
        "scheduling.runner.verify_credit",
        AsyncMock(return_value=BillingResult(verified=True, billing_method="credit")),
    )
    run_once = AsyncMock(
        return_value=AgentRunOnceResult(
            task_state="failed",
            response_state="failed",
            output_text="failed to run",
            duration_ms=25,
            usage_event=SimpleNamespace(cost_usdc=123),
            usage_data={},
            llm_config=None,
            marketplace_stats_billable=False,
        )
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", run_once)
    stored = _stored_result(status="failed", error="Execution failed")
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", AsyncMock(return_value=stored))
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_failed", AsyncMock(return_value=None))

    reserve_mock = AsyncMock()
    post_tweet_mock = AsyncMock()
    monkeypatch.setattr("scheduling.runner.reserve_x_broadcast", reserve_mock)
    monkeypatch.setattr("shared.x_client.post_tweet", post_tweet_mock)

    from scheduling.runner import execute_scheduled_run

    sched = _schedule(callback_format="x")
    result = await execute_scheduled_run(sched)

    assert result.status == "failed"
    reserve_mock.assert_not_awaited()
    post_tweet_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x_broadcast_skips_non_operator_org(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    test_settings.x_broadcast_enabled = True
    test_settings.x_broadcast_org_id = "other-org"
    test_settings.x_api_key = "k"
    test_settings.x_api_secret = "s"
    test_settings.x_access_token = "t"
    test_settings.x_access_token_secret = "ts"
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
            output_text="Clean report summary",
            duration_ms=25,
            usage_event=SimpleNamespace(cost_usdc=123),
            usage_data={},
            llm_config=None,
            marketplace_stats_billable=False,
        )
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", run_once)
    stored = _stored_result(output_text="Clean report summary")
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", AsyncMock(return_value=stored))
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_succeeded", AsyncMock(return_value=None))

    reserve_mock = AsyncMock()
    post_tweet_mock = AsyncMock()
    monkeypatch.setattr("scheduling.runner.reserve_x_broadcast", reserve_mock)
    monkeypatch.setattr("shared.x_client.post_tweet", post_tweet_mock)

    from scheduling.runner import execute_scheduled_run

    sched = _schedule(callback_format="x")
    result = await execute_scheduled_run(sched)

    assert result.status == "completed"
    reserve_mock.assert_not_awaited()
    post_tweet_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x_broadcast_skips_duplicate_reservation(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    test_settings.x_broadcast_enabled = True
    test_settings.x_broadcast_org_id = "org-1"
    test_settings.x_api_key = "k"
    test_settings.x_api_secret = "s"
    test_settings.x_access_token = "t"
    test_settings.x_access_token_secret = "ts"
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
            output_text="Clean report summary",
            duration_ms=25,
            usage_event=SimpleNamespace(cost_usdc=123),
            usage_data={},
            llm_config=None,
            marketplace_stats_billable=False,
        )
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", run_once)
    stored = _stored_result(output_text="Clean report summary")
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", AsyncMock(return_value=stored))
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_succeeded", AsyncMock(return_value=None))

    reserve_mock = AsyncMock(return_value=False)
    post_tweet_mock = AsyncMock()
    monkeypatch.setattr("scheduling.runner.reserve_x_broadcast", reserve_mock)
    monkeypatch.setattr("shared.x_client.post_tweet", post_tweet_mock)

    from scheduling.runner import execute_scheduled_run

    sched = _schedule(callback_format="x")
    result = await execute_scheduled_run(sched)

    assert result.status == "completed"
    reserve_mock.assert_awaited_once()
    post_tweet_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x_broadcast_publication_failure_does_not_fail_run(monkeypatch, test_settings):
    test_settings.scheduled_runs_execution_timeout_seconds = 5
    test_settings.x_broadcast_enabled = True
    test_settings.x_broadcast_org_id = "org-1"
    test_settings.x_api_key = "k"
    test_settings.x_api_secret = "s"
    test_settings.x_access_token = "t"
    test_settings.x_access_token_secret = "ts"
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
            output_text="Clean report summary",
            duration_ms=25,
            usage_event=SimpleNamespace(cost_usdc=123),
            usage_data={},
            llm_config=None,
            marketplace_stats_billable=False,
        )
    )
    monkeypatch.setattr("scheduling.runner.run_agent_once", run_once)
    stored = _stored_result(output_text="Clean report summary")
    monkeypatch.setattr("scheduling.runner.record_scheduled_run_result", AsyncMock(return_value=stored))
    monkeypatch.setattr("scheduling.runner.mark_scheduled_run_succeeded", AsyncMock(return_value=None))

    reserve_mock = AsyncMock(return_value=True)
    post_tweet_mock = AsyncMock(side_effect=RuntimeError("Network timeout"))
    monkeypatch.setattr("scheduling.runner.reserve_x_broadcast", reserve_mock)
    monkeypatch.setattr("shared.x_client.post_tweet", post_tweet_mock)

    from scheduling.runner import execute_scheduled_run

    sched = _schedule(callback_format="x")
    result = await execute_scheduled_run(sched)

    assert result.status == "completed"
    reserve_mock.assert_awaited_once()
    post_tweet_mock.assert_awaited_once()
