from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.research_repo import (
    ResearchToolError,
    build_research_prompt,
    collect_revision_documents,
    collect_source_documents,
    conduct_research,
    find_stale_reports,
    render_report,
    render_source_manifest,
    slugify,
    source_manifest_sha256,
    validate_research_report,
)
from scripts.research_repo_worker import WorkerError, load_request, run_request

VALID_REPORT = """## Executive conclusion
No issue reproduced.
## Findings
None.
## Verified controls
Credit locking exists in `billing/credit.py`.
## Uncertainties and alternatives
Runtime behavior was not exercised.
## Recommended follow-up tests or actions
Run focused billing tests.
## Sources
- `billing/credit.py`
"""


def test_slugify_returns_stable_safe_filename_component() -> None:
    assert slugify("Billing / settlement: replay? bypass!") == "billing-settlement-replay-bypass"
    assert slugify("!!!") == "research"


def test_collect_source_documents_excludes_secrets_and_ignored_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "keys").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / "src" / "module.py").write_text("answer = 42\n", encoding="utf-8")
    (tmp_path / "src" / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "keys" / "private.pem").write_text("private key", encoding="utf-8")
    (tmp_path / ".venv" / "ignored.py").write_text("ignored = True\n", encoding="utf-8")

    documents = collect_source_documents(tmp_path, ["src", "keys", ".venv"])

    assert documents == [("src/module.py", "answer = 42\n")]


def test_collect_source_documents_redacts_embedded_credentials(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.py").write_text(
        'OPENAI_API_KEY = "do-not-upload"\n'
        '"access_token": "also-do-not-upload"\n'
        'header = "Bearer abcdefghijklmnopqrstuvwxyz"\n'
        "tokens_in = 42\n"
        "token_cost = 7\n"
        "auth_token: str | None\n"
        'token = request.headers.get("authorization")\n'
        'setting = "safe"\n',
        encoding="utf-8",
    )

    documents = collect_source_documents(tmp_path, ["src"])

    assert "do-not-upload" not in documents[0][1]
    assert "abcdefghijklmnopqrstuvwxyz" not in documents[0][1]
    assert "tokens_in = 42" in documents[0][1]
    assert "token_cost = 7" in documents[0][1]
    assert "auth_token: str | None" in documents[0][1]
    assert 'token = request.headers.get("authorization")' in documents[0][1]
    assert "OPENAI_API_KEY = <redacted>" in documents[0][1]
    assert 'setting = "safe"' in documents[0][1]


def test_collect_source_documents_excludes_drafts_but_keeps_verified_index(tmp_path: Path) -> None:
    research_directory = tmp_path / "docs" / "research"
    research_directory.mkdir(parents=True)
    (research_directory / "draft.md").write_text("unverified", encoding="utf-8")
    (research_directory / "knowledge-index.md").write_text("verified", encoding="utf-8")

    documents = collect_source_documents(tmp_path, ["docs"])

    assert documents == [("docs/research/knowledge-index.md", "verified")]


def test_collect_source_documents_rejects_scope_outside_repository(tmp_path: Path) -> None:
    with pytest.raises(ResearchToolError, match="inside the repository"):
        collect_source_documents(tmp_path, ["../outside"])


def test_collect_revision_documents_ignores_later_worktree_changes(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "research@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Research Test"], cwd=tmp_path, check=True)
    source_directory = tmp_path / "src"
    source_directory.mkdir()
    source_path = source_directory / "module.py"
    source_path.write_text("value = 'committed'\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/module.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    source_path.write_text("value = 'dirty'\n", encoding="utf-8")

    documents = collect_revision_documents(tmp_path, ["src"], revision)

    assert documents[0][0] == "src/module.py"
    assert "value = 'committed'" in documents[0][1]
    assert "dirty" not in documents[0][1]


def test_render_source_manifest_preserves_paths_and_neutralizes_code_fences() -> None:
    manifest = render_source_manifest([("billing/credit.py", "value = ```\n")], "abc123")

    assert "Evidence revision: `abc123`" in manifest
    assert "## `billing/credit.py`" in manifest
    assert "value = '''" in manifest


def test_build_research_prompt_requires_exact_local_source_bullets() -> None:
    prompt = build_research_prompt("roadmap", "Analyze the product roadmap", "abc123", ["README.md"])

    assert "## Sources" in prompt
    assert "exact collected repository path in backticks" in prompt
    assert "do not cite `repo-source.md` as a substitute" in prompt


def test_source_manifest_hash_tracks_exact_sanitized_payload() -> None:
    documents = [("billing/credit.py", "value = 1\n")]

    assert source_manifest_sha256(documents, "abc123") == source_manifest_sha256(documents, "abc123")
    assert source_manifest_sha256(documents, "abc123") != source_manifest_sha256(documents, "def456")
    assert source_manifest_sha256([], "abc123") is None


def test_render_report_marks_output_as_draft_and_redacts_assignments() -> None:
    report = render_report(
        topic="security",
        query="Audit billing",
        revision="abc123",
        report_source="local",
        evidence_dirty=True,
        manifest_sha256="manifest-hash",
        redaction_count=2,
        generated_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        source_paths=["billing/credit.py"],
        report="OPENAI_API_KEY=do-not-persist\n## Findings\nNo issue reproduced.",
    )

    assert "status: draft" in report
    assert 'evidence_commit: "abc123"' in report
    assert "evidence_dirty: true" in report
    assert 'source_manifest_sha256: "manifest-hash"' in report
    assert "source_redaction_count: 2" in report
    assert 'report_source: "local"' in report
    assert "prompt_version: 1" in report
    assert "do-not-persist" not in report
    assert "<redacted>" in report


def test_conduct_research_passes_only_manifest_directory_to_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_create_subprocess_exec(*command: str, **_: object) -> FakeProcess:
        request_path = Path(command[command.index("--request") + 1])
        output_path = Path(command[command.index("--output") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        document_path = Path(request["doc_path"])
        assert document_path == request_path.parent / "documents"
        assert (document_path / "repo-source.md").is_file()
        assert "GITHUB_TOKEN" not in request_path.read_text(encoding="utf-8")
        assert request["max_subtopics"] == 3
        output_path.write_text(VALID_REPORT, encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    report = asyncio.run(
        conduct_research(
            "Audit billing",
            report_source="local",
            documents=[("billing/credit.py", "value = 1")],
            revision="abc123",
            researcher_python=Path("research-python"),
            github_mcp=False,
            timeout_seconds=30,
            max_subtopics=3,
        )
    )

    assert report == VALID_REPORT.strip()


def test_worker_passes_parent_prompt_as_custom_report_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeResearcher:
        def __init__(self, **_: object) -> None:
            pass

        async def conduct_research(self) -> None:
            return None

        async def write_report(self, **kwargs: object) -> str:
            assert kwargs == {"custom_prompt": "Analyze the roadmap"}
            return VALID_REPORT

    monkeypatch.setitem(sys.modules, "gpt_researcher", SimpleNamespace(GPTResearcher=FakeResearcher))
    document_directory = tmp_path / "documents"
    document_directory.mkdir()
    request = {
        "prompt": "Analyze the roadmap",
        "report_source": "local",
        "doc_path": str(document_directory),
        "github_mcp": False,
        "max_subtopics": 3,
    }

    output_path = tmp_path / "report.md"
    asyncio.run(run_request(request, output_path))

    assert output_path.read_text(encoding="utf-8") == VALID_REPORT.strip() + "\n"


def test_conduct_research_terminates_timed_out_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        returncode = None
        killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Future()
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = -1
            return -1

    process = FakeProcess()

    async def fake_create_subprocess_exec(*_: str, **__: object) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    with pytest.raises(ResearchToolError, match="timed out"):
        asyncio.run(
            conduct_research(
                "Audit billing",
                report_source="local",
                documents=[("billing/credit.py", "value = 1")],
                revision="abc123",
                researcher_python=Path("research-python"),
                github_mcp=False,
                timeout_seconds=0.001,
                max_subtopics=3,
            )
        )

    assert process.killed is True


def test_conduct_research_redacts_failed_worker_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"OPENAI_API_KEY=do-not-return\n"

    async def fake_create_subprocess_exec(*_: str, **__: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    with pytest.raises(ResearchToolError) as captured:
        asyncio.run(
            conduct_research(
                "Audit billing",
                report_source="local",
                documents=[("billing/credit.py", "value = 1")],
                revision="abc123",
                researcher_python=Path("research-python"),
                github_mcp=False,
                timeout_seconds=30,
                max_subtopics=3,
            )
        )

    assert "do-not-return" not in str(captured.value)
    assert "<redacted>" in str(captured.value)


def test_validate_research_report_rejects_missing_sections_and_local_citations() -> None:
    with pytest.raises(ResearchToolError, match="missing required sections"):
        validate_research_report("## Findings\nNone", [])

    with pytest.raises(ResearchToolError, match="does not cite"):
        validate_research_report(VALID_REPORT.replace("billing/credit.py", "external source"), ["billing/credit.py"])

    with pytest.raises(ResearchToolError, match="does not cite"):
        report_with_url_only = VALID_REPORT.replace("- `billing/credit.py`", "https://example.com/billing/credit.py")
        validate_research_report(report_with_url_only, ["billing/credit.py"])


def test_worker_rejects_local_request_without_documents(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps({"prompt": "Audit billing", "report_source": "local", "github_mcp": False}),
        encoding="utf-8",
    )

    with pytest.raises(WorkerError, match="requires a document directory"):
        load_request(request_path)


def test_worker_rejects_documents_outside_request_directory(tmp_path: Path) -> None:
    outside_directory = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_directory.mkdir()
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "prompt": "Audit billing",
                "report_source": "local",
                "doc_path": str(outside_directory),
                "github_mcp": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkerError, match="inside the request directory"):
        load_request(request_path)


def test_find_stale_reports_flags_commit_drift_and_dirty_evidence(tmp_path: Path) -> None:
    security_directory = tmp_path / "security"
    security_directory.mkdir()
    (security_directory / "current.md").write_text(
        '---\nevidence_commit: "current"\nevidence_dirty: false\n---\n', encoding="utf-8"
    )
    (security_directory / "old.md").write_text('---\nevidence_commit: "old"\nevidence_dirty: false\n---\n', encoding="utf-8")
    (security_directory / "dirty.md").write_text('---\nevidence_commit: "current"\nevidence_dirty: true\n---\n', encoding="utf-8")
    (security_directory / "superseded.md").write_text(
        '---\nstatus: "superseded"\nevidence_commit: "old"\nevidence_dirty: false\n---\n', encoding="utf-8"
    )

    stale = find_stale_reports(tmp_path, "current")

    assert [(path.name, reason) for path, reason in stale] == [
        ("dirty.md", "evidence was dirty or lacks clean-tree provenance"),
        ("old.md", "evidence commit old differs from current"),
    ]
