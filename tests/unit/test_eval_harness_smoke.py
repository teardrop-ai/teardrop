# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from pathlib import Path

from evals.runner import RunArtifact, load_tasks, run_suite
from evals.scorer import score_json_shape


def test_json_shape_scoring_extracts_object_before_summary():
    actual = '{"task_class":"entry_timing","schema_version":1}\n---\n## Report'

    assert score_json_shape({"task_class": None, "schema_version": None}, actual) == 1.0


async def test_forbidden_tool_calls_fail_task():
    tasks = load_tasks(Path(__file__).resolve().parents[2] / "evals" / "tasks" / "tool_discovery.yaml")
    task = next(task for task in tasks if task.id == "tool_discovery.portfolio.001")

    async def _fake_runner(_task):
        return RunArtifact(
            text="Portfolio holdings and USD value are available.",
            tool_names_used=["get_wallet_portfolio", "get_eth_balance"],
            duration_ms=100,
        )

    report = await run_suite(suite_name="tool_discovery", tasks=[task], run_task=_fake_runner)

    assert report.total_tasks == 1
    assert report.passed_tasks == 0


async def test_eval_harness_smoke_suite_runs():
    suite_path = Path(__file__).resolve().parents[2] / "evals" / "tasks" / "smoke.yaml"
    tasks = load_tasks(suite_path)

    async def _fake_runner(task):
        if "datetime" in task.id:
            return RunArtifact(text="Current UTC time is 2026-01-01T00:00:00Z", tool_names_used=["get_datetime"], duration_ms=100)
        if "wallet" in task.id:
            return RunArtifact(text="ETH balance is 0.0 ETH", tool_names_used=["get_eth_balance"], duration_ms=100)
        return RunArtifact(text="ETH price in USD is 3000.", tool_names_used=["get_token_price"], duration_ms=100)

    report = await run_suite(suite_name="smoke", tasks=tasks, run_task=_fake_runner)

    assert report.total_tasks == 3
    assert report.passed_tasks == 3
