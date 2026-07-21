# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""CLI entrypoint for eval harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from evals.policy import EvalPolicy, check_policy
from evals.runner import EvalReport, EvalTask, RunArtifact, load_tasks, render_markdown_report, run_suite

_SAFE_TASK_CLASS_RE = re.compile(r"^[a-z0-9_]{1,60}$")
_SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_./:-]{1,200}$")
_PROMOTION_SOURCES = frozenset({"api", "schedule", "trigger", "a2a"})

_PROMOTION_CANDIDATES_SQL = """
    WITH canonical_usage AS (
        SELECT DISTINCT ON (run_id, org_id)
            run_id, org_id, source, provider, model, runner_version, duration_ms, cost_usdc, created_at
        FROM usage_events
        WHERE run_id <> ''
        ORDER BY run_id, org_id, created_at DESC
    )
    SELECT
        d.run_id,
        d.task_class,
        d.tool_names,
        d.outcome_source,
        d.confidence,
        d.created_at,
        u.source,
        u.provider,
        u.model,
        u.runner_version,
        u.duration_ms,
        u.cost_usdc
    FROM run_decisions d
        JOIN canonical_usage u ON u.run_id = d.run_id AND u.org_id = d.org_id
    WHERE d.outcome = 1
      AND d.outcome_source IN ('explicit', 'feedback')
      AND d.created_at >= NOW() - ($1 * INTERVAL '1 day')
            AND d.org_id = $3
    ORDER BY
        CASE d.outcome_source WHEN 'explicit' THEN 0 ELSE 1 END,
        d.confidence DESC NULLS LAST,
        d.created_at DESC
    LIMIT $2
"""


class EvalPromotionCandidate(BaseModel):
    """Sanitized metadata for human review before writing a runnable eval task."""

    run_id: str = Field(min_length=1, max_length=128)
    source: str = ""
    task_class: str = ""
    tool_names: list[str] = Field(default_factory=list)
    outcome_source: str
    confidence: float | None = None
    provider: str = ""
    model: str = ""
    runner_version: str = ""
    duration_ms: int = 0
    cost_usdc: int = 0
    created_at: datetime


class EvalPromotionQueue(BaseModel):
    """Non-runnable review queue; it deliberately contains no conversation data."""

    schema_version: int = 1
    generated_at: datetime
    review_required: bool = True
    candidates: list[EvalPromotionCandidate]


def _resolve_suite_path(suite: str) -> Path:
    candidate = Path(suite)
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parent / "tasks" / f"{suite}.yaml"


def _safe_task_class(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if _SAFE_TASK_CLASS_RE.fullmatch(normalized) else ""


def _safe_tool_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tool_names: list[str] = []
    for raw_name in value:
        name = str(raw_name).strip()
        if name and _SAFE_TOOL_NAME_RE.fullmatch(name) and name not in tool_names:
            tool_names.append(name)
    return tool_names[:50]


def _promotion_candidate_from_row(row: dict[str, Any]) -> EvalPromotionCandidate:
    source = str(row.get("source") or "")
    return EvalPromotionCandidate(
        run_id=str(row["run_id"])[:128],
        source=source if source in _PROMOTION_SOURCES else "",
        task_class=_safe_task_class(row.get("task_class")),
        tool_names=_safe_tool_names(row.get("tool_names")),
        outcome_source=str(row["outcome_source"]),
        confidence=float(row["confidence"]) if row.get("confidence") is not None else None,
        provider=str(row.get("provider") or "")[:120],
        model=str(row.get("model") or "")[:200],
        runner_version=str(row.get("runner_version") or "")[:64],
        duration_ms=max(0, int(row.get("duration_ms") or 0)),
        cost_usdc=max(0, int(row.get("cost_usdc") or 0)),
        created_at=row["created_at"],
    )


async def _query_promotion_candidates(
    connection: Any,
    *,
    org_id: str,
    lookback_days: int,
    limit: int,
) -> list[EvalPromotionCandidate]:
    """Fetch human-positive metadata only; prompts, outputs, and identities stay out of the queue."""
    rows = await connection.fetch(_PROMOTION_CANDIDATES_SQL, lookback_days, limit, org_id)
    return [_promotion_candidate_from_row(dict(row)) for row in rows]


def _render_promotion_queue(candidates: list[EvalPromotionCandidate]) -> str:
    queue = EvalPromotionQueue(
        generated_at=datetime.now(timezone.utc),
        candidates=candidates,
    )
    return queue.model_dump_json(indent=2)


def _bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"must be an integer from {minimum} to {maximum}") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"must be from {minimum} to {maximum}")
        return parsed

    return parse


def _non_empty_argument(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("must not be empty")
    return normalized


def _build_promotion_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export sanitized, human-reviewed production eval candidates")
    parser.add_argument(
        "--pg-dsn",
        default=os.getenv("PG_DSN") or os.getenv("DATABASE_URL", ""),
        help="Postgres DSN; defaults to PG_DSN or DATABASE_URL",
    )
    parser.add_argument("--org-id", required=True, type=_non_empty_argument, help="Organization ID to export")
    parser.add_argument("--lookback-days", type=_bounded_int(1, 365), default=30)
    parser.add_argument("--limit", type=_bounded_int(1, 200), default=25)
    parser.add_argument("--output", default="", help="Optional JSON review-queue path; stdout when omitted")
    return parser


async def _run_promotion(args: argparse.Namespace) -> int:
    if not args.pg_dsn:
        print("promote requires --pg-dsn or PG_DSN/DATABASE_URL", file=sys.stderr)
        return 2
    org_id = str(getattr(args, "org_id", "")).strip()
    if not org_id:
        print("promote requires --org-id", file=sys.stderr)
        return 2

    import asyncpg

    connection = await asyncpg.connect(args.pg_dsn)
    try:
        async with connection.transaction(readonly=True):
            candidates = await _query_promotion_candidates(
                connection,
                org_id=org_id,
                lookback_days=args.lookback_days,
                limit=args.limit,
            )
    finally:
        await connection.close()

    rendered = _render_promotion_queue(candidates)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


async def _run_task_http(task: EvalTask, *, base_url: str, token: str | None) -> RunArtifact:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    user_msg = next((m.content for m in task.messages if m.role == "user"), task.messages[-1].content)
    payload = {
        "message": user_msg,
        "thread_id": f"eval-{task.id}",
        "emit_ui": False,
    }

    text_parts: list[str] = []
    tool_names: list[str] = []
    usage: dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"{base_url.rstrip('/')}/agent/run", json=payload, headers=headers) as resp:
            resp.raise_for_status()
            current_event = ""
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line.split(":", 1)[1].strip()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if current_event == "TEXT_MESSAGE_CONTENT":
                    delta = str(data.get("delta", ""))
                    if delta:
                        text_parts.append(delta)
                elif current_event == "TOOL_CALL_START":
                    name = str(data.get("tool_name", ""))
                    if name:
                        tool_names.append(name)
                elif current_event == "USAGE_SUMMARY":
                    usage = data

    return RunArtifact(
        text="".join(text_parts),
        tool_names_used=tool_names,
        tokens_in=int(usage.get("tokens_in", 0)),
        tokens_out=int(usage.get("tokens_out", 0)),
        cache_read_input_tokens=int(usage.get("cache_read_tokens", 0)),
        cache_creation_input_tokens=int(usage.get("cache_creation_tokens", 0)),
        duration_ms=int(usage.get("duration_ms", 0)),
        cost_usdc=int(usage.get("cost_usdc", 0)),
    )


def _load_report(path: Path) -> EvalReport:
    return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))


def _load_policy(path: Path) -> EvalPolicy:
    return EvalPolicy.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _print_policy_violations(violations: list[Any]) -> None:
    print("Policy violations:", file=sys.stderr)
    for violation in violations:
        print(
            f"- {violation.rule}: expected {violation.expected}, actual {violation.actual}",
            file=sys.stderr,
        )


async def _main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "promote":
        return await _run_promotion(_build_promotion_parser().parse_args(argv[1:]))

    parser = argparse.ArgumentParser(description="Run Teardrop agent eval suite")
    parser.add_argument("--suite", required=True, help="Suite name under evals/tasks or an explicit path")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Teardrop API base URL")
    parser.add_argument("--token", default=os.getenv("TEARDROP_EVAL_TOKEN", ""), help="Bearer token")
    parser.add_argument("--baseline-report", default="", help="Optional baseline report JSON path")
    parser.add_argument("--policy-file", default="", help="Optional policy JSON path")
    parser.add_argument(
        "--fail-on-regression",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Exit non-zero on policy violations. Use --no-fail-on-regression to warn only.",
    )
    parser.add_argument(
        "--judge-model",
        default="claude-haiku-4-5-20251001",
        help="Anthropic model used for llm_judge eval tasks",
    )
    parser.add_argument("--output", default="", help="Optional path to write report JSON")
    args = parser.parse_args(argv)

    suite_path = _resolve_suite_path(args.suite)
    tasks = load_tasks(suite_path)
    baseline = _load_report(Path(args.baseline_report)) if args.baseline_report else None

    report = await run_suite(
        suite_name=suite_path.stem,
        tasks=tasks,
        run_task=lambda task: _run_task_http(task, base_url=args.base_url, token=args.token or None),
        judge_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        judge_model=args.judge_model,
    )

    print(render_markdown_report(report, baseline=baseline))

    if args.policy_file:
        violations = check_policy(report, _load_policy(Path(args.policy_file)), baseline=baseline)
        if violations:
            _print_policy_violations(violations)
            if args.fail_on_regression:
                return 1

    if args.output:
        Path(args.output).write_text(report.model_dump_json(indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
