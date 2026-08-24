# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Run GPT-Researcher from the isolated research virtual environment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class WorkerError(RuntimeError):
    """Raised when the isolated research request is invalid or cannot run."""


def _path_inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise WorkerError(f"{label} must stay inside the request directory.") from exc
    return resolved


@contextmanager
def _temporary_environment(updates: dict[str, str | None]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_request(path: Path) -> dict[str, object]:
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError(f"Could not read research request: {path}") from exc
    if not isinstance(request, dict):
        raise WorkerError("Research request must be a JSON object.")

    prompt = request.get("prompt")
    report_source = request.get("report_source")
    if not isinstance(prompt, str) or not prompt.strip():
        raise WorkerError("Research request must include a non-empty prompt.")
    if report_source not in {"local", "web", "hybrid"}:
        raise WorkerError("Research request has an unsupported report source.")

    doc_path = request.get("doc_path")
    if doc_path is not None:
        if not isinstance(doc_path, str):
            raise WorkerError("Research request doc_path must be a string.")
        document_directory = _path_inside(Path(doc_path), path.parent, "Research document directory")
        if not document_directory.is_dir():
            raise WorkerError(f"Research document directory does not exist: {document_directory}")
        request["doc_path"] = str(document_directory)
    if report_source in {"local", "hybrid"} and not doc_path:
        raise WorkerError(f"{report_source} research requires a document directory.")

    github_mcp = request.get("github_mcp", False)
    if not isinstance(github_mcp, bool):
        raise WorkerError("Research request github_mcp must be a boolean.")
    if github_mcp and report_source == "local":
        raise WorkerError("GitHub MCP cannot be used with local-only research.")
    max_subtopics = request.get("max_subtopics", 5)
    if not isinstance(max_subtopics, int) or isinstance(max_subtopics, bool) or not 1 <= max_subtopics <= 10:
        raise WorkerError("Research request max_subtopics must be an integer between 1 and 10.")
    request["max_subtopics"] = max_subtopics
    return request


def build_github_mcp_config(enabled: bool) -> list[dict[str, object]]:
    if not enabled:
        return []
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise WorkerError("--github-mcp requires GITHUB_TOKEN in the environment.")
    return [
        {
            "name": "github",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": token},
        }
    ]


def _retriever_value(github_mcp: bool) -> str | None:
    if not github_mcp:
        return os.getenv("RETRIEVER")
    configured = os.getenv("RETRIEVER", "tavily")
    retrievers = [item.strip() for item in configured.split(",") if item.strip()]
    if "mcp" not in retrievers:
        retrievers.append("mcp")
    return ",".join(retrievers)


async def run_request(request: dict[str, object], output_path: Path) -> None:
    try:
        from gpt_researcher import GPTResearcher
    except ImportError as exc:
        raise WorkerError(
            "GPT-Researcher is unavailable in the research environment. Install requirements-research.txt there."
        ) from exc

    report_source = str(request["report_source"])
    doc_path = request.get("doc_path")
    updates = {
        "REPORT_SOURCE": report_source,
        "DOC_PATH": str(doc_path) if doc_path else None,
        "RETRIEVER": _retriever_value(bool(request["github_mcp"])),
    }
    mcp_configs = build_github_mcp_config(bool(request["github_mcp"]))
    with _temporary_environment(updates):
        researcher = GPTResearcher(
            query=str(request["prompt"]),
            report_source=report_source,
            mcp_configs=mcp_configs or None,
            max_subtopics=int(request["max_subtopics"]),
        )
        await researcher.conduct_research()
        report = await researcher.write_report(custom_prompt=str(request["prompt"]))

    if not isinstance(report, str) or not report.strip():
        raise WorkerError("GPT-Researcher returned an empty report.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.strip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        request = load_request(args.request)
        output_path = _path_inside(args.output, args.request.parent, "Research output")
        asyncio.run(run_request(request, output_path))
    except WorkerError as exc:
        print(f"Research worker failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
