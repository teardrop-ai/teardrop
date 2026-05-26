from __future__ import annotations

from pathlib import Path

from evals.runner import RunArtifact, load_tasks, run_suite


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
