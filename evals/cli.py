# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""CLI entrypoint for eval harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from evals.runner import EvalReport, EvalTask, RunArtifact, load_tasks, render_markdown_report, run_suite


def _resolve_suite_path(suite: str) -> Path:
    candidate = Path(suite)
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parent / "tasks" / f"{suite}.yaml"


async def _run_task_http(task: EvalTask, *, base_url: str, token: str | None) -> RunArtifact:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    user_msg = next((m.content for m in task.messages if m.role == "user"), task.messages[-1].content)
    payload = {
        "message": user_msg,
        "thread_id": f"eval-{task.id}",
        "emit_ui": True,
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


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Run Teardrop agent eval suite")
    parser.add_argument("--suite", required=True, help="Suite name under evals/tasks or an explicit path")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Teardrop API base URL")
    parser.add_argument("--token", default=os.getenv("TEARDROP_EVAL_TOKEN", ""), help="Bearer token")
    parser.add_argument("--baseline-report", default="", help="Optional baseline report JSON path")
    parser.add_argument("--output", default="", help="Optional path to write report JSON")
    args = parser.parse_args()

    suite_path = _resolve_suite_path(args.suite)
    tasks = load_tasks(suite_path)

    report = await run_suite(
        suite_name=suite_path.stem,
        tasks=tasks,
        run_task=lambda task: _run_task_http(task, base_url=args.base_url, token=args.token or None),
    )

    baseline = _load_report(Path(args.baseline_report)) if args.baseline_report else None
    print(render_markdown_report(report, baseline=baseline))

    if args.output:
        Path(args.output).write_text(report.model_dump_json(indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(_main())
