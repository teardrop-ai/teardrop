# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Policy checks for eval harness reports."""

from __future__ import annotations

from pydantic import BaseModel, Field

from evals.runner import EvalReport, diff_reports


class EvalPolicy(BaseModel):
    min_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    max_avg_cost_usdc: int = Field(default=0, ge=0)
    max_avg_duration_ms: float = Field(default=0.0, ge=0.0)
    max_score_regression: float = Field(default=0.05, ge=0.0)
    max_cost_regression_pct: float = Field(default=20.0, ge=0.0)
    max_latency_regression_pct: float = Field(default=30.0, ge=0.0)


class PolicyViolation(BaseModel):
    rule: str
    expected: str
    actual: str


def _pass_rate(report: EvalReport) -> float:
    if report.total_tasks <= 0:
        return 0.0
    return report.passed_tasks / report.total_tasks


def check_policy(
    report: EvalReport,
    policy: EvalPolicy,
    baseline: EvalReport | None = None,
) -> list[PolicyViolation]:
    violations: list[PolicyViolation] = []

    pass_rate = _pass_rate(report)
    if pass_rate < policy.min_pass_rate:
        violations.append(
            PolicyViolation(
                rule="min_pass_rate",
                expected=f">= {policy.min_pass_rate:.2f}",
                actual=f"{pass_rate:.2f}",
            )
        )

    if policy.max_avg_cost_usdc > 0 and report.avg_cost_usdc > policy.max_avg_cost_usdc:
        violations.append(
            PolicyViolation(
                rule="max_avg_cost_usdc",
                expected=f"<= {policy.max_avg_cost_usdc}",
                actual=f"{report.avg_cost_usdc:.2f}",
            )
        )

    if policy.max_avg_duration_ms > 0 and report.avg_duration_ms > policy.max_avg_duration_ms:
        violations.append(
            PolicyViolation(
                rule="max_avg_duration_ms",
                expected=f"<= {policy.max_avg_duration_ms:.2f}",
                actual=f"{report.avg_duration_ms:.2f}",
            )
        )

    if baseline is None:
        return violations

    diff = diff_reports(baseline, report)
    if diff.score_delta < -policy.max_score_regression:
        violations.append(
            PolicyViolation(
                rule="max_score_regression",
                expected=f">= {-policy.max_score_regression:.4f}",
                actual=f"{diff.score_delta:.4f}",
            )
        )

    if diff.cost_delta_pct > policy.max_cost_regression_pct:
        violations.append(
            PolicyViolation(
                rule="max_cost_regression_pct",
                expected=f"<= {policy.max_cost_regression_pct:.2f}%",
                actual=f"{diff.cost_delta_pct:.2f}%",
            )
        )

    if diff.latency_delta_pct > policy.max_latency_regression_pct:
        violations.append(
            PolicyViolation(
                rule="max_latency_regression_pct",
                expected=f"<= {policy.max_latency_regression_pct:.2f}%",
                actual=f"{diff.latency_delta_pct:.2f}%",
            )
        )

    return violations
