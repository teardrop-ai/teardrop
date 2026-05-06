# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Eval harness runner for task-level agent quality/cost/latency checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from evals.scorer import score_task


class EvalMessage(BaseModel):
    role: str
    content: str


class EvalTask(BaseModel):
    id: str
    messages: list[EvalMessage]
    expected_tool_calls: list[str] = Field(default_factory=list)
    expected_text_contains: list[str] = Field(default_factory=list)
    max_duration_ms: int = 0
    max_cost_usdc: int = 0
    scorer: str = "contains"
    rubric: str = ""


class RunArtifact(BaseModel):
    text: str = ""
    tool_names_used: list[str] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    tool_iterations: int = 0
    duration_ms: int = 0
    cost_usdc: int = 0


class EvalTaskResult(BaseModel):
    id: str
    score: float
    passed: bool
    duration_ms: int
    cost_usdc: int
    tool_names_used: list[str]
    tokens_in: int
    tokens_out: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


class EvalReport(BaseModel):
    suite: str
    total_tasks: int
    passed_tasks: int
    avg_score: float
    avg_duration_ms: float
    avg_cost_usdc: float
    tasks: list[EvalTaskResult]


RunTaskCallable = Callable[[EvalTask], Awaitable[RunArtifact]]


@dataclass
class EvalDiff:
    latency_delta_pct: float
    cost_delta_pct: float
    score_delta: float


def _pct_delta(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100.0


def diff_reports(baseline: EvalReport, candidate: EvalReport) -> EvalDiff:
    return EvalDiff(
        latency_delta_pct=_pct_delta(baseline.avg_duration_ms, candidate.avg_duration_ms),
        cost_delta_pct=_pct_delta(baseline.avg_cost_usdc, candidate.avg_cost_usdc),
        score_delta=candidate.avg_score - baseline.avg_score,
    )


def _load_yaml_or_json(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8")
    # JSON is valid YAML; try JSON first to avoid extra dependencies.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise RuntimeError("YAML parser unavailable. Install PyYAML or provide JSON-formatted suite files.") from exc

    return yaml.safe_load(raw)


def load_tasks(path: Path) -> list[EvalTask]:
    payload = _load_yaml_or_json(path)
    if isinstance(payload, dict):
        if "tasks" in payload:
            payload = payload["tasks"]
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("Task suite must be a list of task objects")
    return [EvalTask.model_validate(item) for item in payload]


async def run_suite(
    *,
    suite_name: str,
    tasks: list[EvalTask],
    run_task: RunTaskCallable,
) -> EvalReport:
    results: list[EvalTaskResult] = []

    for task in tasks:
        artifact = await run_task(task)
        score = score_task(
            scorer=task.scorer,
            expected_text_contains=task.expected_text_contains,
            actual_text=artifact.text,
        )
        tool_call_ok = True
        if task.expected_tool_calls:
            used = set(artifact.tool_names_used)
            tool_call_ok = all(name in used for name in task.expected_tool_calls)

        duration_ok = task.max_duration_ms <= 0 or artifact.duration_ms <= task.max_duration_ms
        cost_ok = task.max_cost_usdc <= 0 or artifact.cost_usdc <= task.max_cost_usdc
        passed = score >= 0.8 and tool_call_ok and duration_ok and cost_ok

        results.append(
            EvalTaskResult(
                id=task.id,
                score=round(score, 4),
                passed=passed,
                duration_ms=artifact.duration_ms,
                cost_usdc=artifact.cost_usdc,
                tool_names_used=artifact.tool_names_used,
                tokens_in=artifact.tokens_in,
                tokens_out=artifact.tokens_out,
                cache_read_input_tokens=artifact.cache_read_input_tokens,
                cache_creation_input_tokens=artifact.cache_creation_input_tokens,
            )
        )

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    avg_score = (sum(r.score for r in results) / total) if total else 0.0
    avg_duration_ms = (sum(r.duration_ms for r in results) / total) if total else 0.0
    avg_cost_usdc = (sum(r.cost_usdc for r in results) / total) if total else 0.0

    return EvalReport(
        suite=suite_name,
        total_tasks=total,
        passed_tasks=passed,
        avg_score=round(avg_score, 4),
        avg_duration_ms=round(avg_duration_ms, 2),
        avg_cost_usdc=round(avg_cost_usdc, 2),
        tasks=results,
    )


def render_markdown_report(report: EvalReport, baseline: EvalReport | None = None) -> str:
    lines = [
        f"# Eval Report - {report.suite}",
        "",
        f"- Total tasks: {report.total_tasks}",
        f"- Passed tasks: {report.passed_tasks}",
        f"- Avg score: {report.avg_score}",
        f"- Avg duration_ms: {report.avg_duration_ms}",
        f"- Avg cost_usdc: {report.avg_cost_usdc}",
    ]
    if baseline is not None:
        diff = diff_reports(baseline, report)
        lines.extend(
            [
                "",
                "## Delta vs baseline",
                f"- latency_delta_pct: {diff.latency_delta_pct:.2f}%",
                f"- cost_delta_pct: {diff.cost_delta_pct:.2f}%",
                f"- score_delta: {diff.score_delta:+.4f}",
            ]
        )

    lines.append("")
    lines.append("## Tasks")
    for task in report.tasks:
        lines.append(
            f"- {task.id}: passed={task.passed} score={task.score} duration_ms={task.duration_ms} cost_usdc={task.cost_usdc}"
        )

    return "\n".join(lines)
