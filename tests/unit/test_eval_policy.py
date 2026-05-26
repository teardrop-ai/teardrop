from __future__ import annotations

from evals.policy import EvalPolicy, check_policy
from evals.runner import EvalReport, EvalTaskResult


def _report(
    *,
    suite: str = "smoke",
    total_tasks: int = 2,
    passed_tasks: int = 2,
    avg_score: float = 1.0,
    avg_duration_ms: float = 100.0,
    avg_cost_usdc: float = 1000.0,
) -> EvalReport:
    return EvalReport(
        suite=suite,
        total_tasks=total_tasks,
        passed_tasks=passed_tasks,
        avg_score=avg_score,
        avg_duration_ms=avg_duration_ms,
        avg_cost_usdc=avg_cost_usdc,
        tasks=[
            EvalTaskResult(
                id="task.001",
                score=avg_score,
                passed=passed_tasks > 0,
                duration_ms=int(avg_duration_ms),
                cost_usdc=int(avg_cost_usdc),
                tool_names_used=[],
                tokens_in=0,
                tokens_out=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            )
        ],
    )


def test_check_policy_flags_pass_rate_and_absolute_limits():
    report = _report(passed_tasks=1, avg_duration_ms=250.0, avg_cost_usdc=1500.0)
    policy = EvalPolicy(min_pass_rate=1.0, max_avg_duration_ms=200.0, max_avg_cost_usdc=1000)

    violations = check_policy(report, policy)

    assert [violation.rule for violation in violations] == [
        "min_pass_rate",
        "max_avg_cost_usdc",
        "max_avg_duration_ms",
    ]


def test_check_policy_flags_regressions_against_baseline():
    baseline = _report(avg_score=0.95, avg_duration_ms=100.0, avg_cost_usdc=1000.0)
    report = _report(avg_score=0.80, avg_duration_ms=140.0, avg_cost_usdc=1300.0)
    policy = EvalPolicy(
        min_pass_rate=0.5,
        max_score_regression=0.05,
        max_cost_regression_pct=20.0,
        max_latency_regression_pct=30.0,
    )

    violations = check_policy(report, policy, baseline=baseline)

    assert [violation.rule for violation in violations] == [
        "max_score_regression",
        "max_cost_regression_pct",
        "max_latency_regression_pct",
    ]


def test_check_policy_returns_no_regression_violations_without_baseline():
    report = _report(avg_score=0.80, avg_duration_ms=140.0, avg_cost_usdc=1300.0)
    policy = EvalPolicy(min_pass_rate=0.5, max_avg_cost_usdc=2000, max_avg_duration_ms=200.0)

    violations = check_policy(report, policy)

    assert violations == []


def test_check_policy_handles_empty_reports_without_dividing_by_zero():
    report = EvalReport(
        suite="empty",
        total_tasks=0,
        passed_tasks=0,
        avg_score=0.0,
        avg_duration_ms=0.0,
        avg_cost_usdc=0.0,
        tasks=[],
    )
    policy = EvalPolicy(min_pass_rate=0.0)

    violations = check_policy(report, policy)

    assert violations == []
