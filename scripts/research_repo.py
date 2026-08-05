"""Run cited repository, roadmap, or competitive research for coding agents.

The command writes draft reports to ``docs/research``. It is intentionally
separate from Teardrop's runtime agent, billing, and scheduling paths.

Examples:
    .venv\\Scripts\\python -m scripts.research_repo --topic security \\
        --query "Can an untrusted caller bypass billing or settlement controls?"
    .venv\\Scripts\\python -m scripts.research_repo --topic competitive \\
        --query "Compare open-source agent research platforms" --github-mcp
    .venv\\Scripts\\python -m scripts.research_repo --topic security \\
        --query "Audit billing" --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT = REPO_ROOT / "docs" / "research"
MAX_FILE_BYTES = 160_000
MAX_SOURCE_BYTES = 1_500_000
DEFAULT_TIMEOUT_SECONDS = 1_800.0
DEFAULT_MAX_SUBTOPICS = 5
MAX_QUERY_CHARS = 800
MAX_QUERY_WORDS = 100
PROMPT_VERSION = 2
IGNORED_DIRECTORIES = {".git", ".venv", ".research-venv", "venv", "__pycache__", "node_modules", "dist", "build"}
SENSITIVE_DIRECTORY_NAMES = {"keys", ".secrets"}
SENSITIVE_FILE_NAMES = {".env", ".env.local", ".env.production", "private.pem", "secret.pem"}
ALLOWED_SUFFIXES = {
    ".css",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
REQUIRED_REPORT_HEADINGS = (
    "## Executive conclusion",
    "## Findings",
    "## Verified controls",
    "## Uncertainties and alternatives",
    "## Recommended follow-up tests or actions",
    "## Sources",
)
IMPROVEMENT_REPORT_HEADINGS = (
    "## Executive conclusion",
    "## Improvement candidates",
    "## Current behavior and evidence",
    "## Proposed incremental changes",
    "## Dependencies and prioritization",
    "## Verification and rollout plan",
    "## Uncertainties and alternatives",
    "## Sources",
)


@dataclass(frozen=True)
class TopicConfig:
    label: str
    directory: str
    report_source: str
    default_scopes: tuple[str, ...]
    focus: str
    required_headings: tuple[str, ...] = REQUIRED_REPORT_HEADINGS
    report_guidance: str = ""


TOPICS: dict[str, TopicConfig] = {
    "security": TopicConfig(
        label="Security architecture audit",
        directory="security",
        report_source="local",
        default_scopes=(
            "billing",
            "marketplace",
            "mcp_client",
            "org_tools",
            "teardrop/app.py",
            "teardrop/a2a_client.py",
            "teardrop/mcp_gateway.py",
            "migrations/versions",
            ".github/skills/teardrop-domain-invariants/SKILL.md",
        ),
        focus=(
            "Audit live code for billing and architecture exploits. Check atomic BIGINT USDC values, "
            "auth_method to billing_method routing, credit SELECT FOR UPDATE and spending limits, "
            "x402 and Stripe replay/double-settlement protections, SSRF validation, org scoping, "
            "secret leakage, pricing/cache precedence, MCP/A2A boundaries, and additive migrations."
        ),
    ),
    "roadmap": TopicConfig(
        label="Product roadmap research",
        directory="roadmap",
        report_source="local",
        default_scopes=("README.md", "docs", "notes", "MARKETPLACE_ONBOARDING.md"),
        focus=(
            "Identify user problems, existing capabilities, explicit planned work, dependencies, "
            "and evidence for prioritization. Separate shipped behavior from proposals and stale notes."
        ),
    ),
    "improvements": TopicConfig(
        label="Product improvement research",
        directory="improvements",
        report_source="local",
        default_scopes=(
            "agent",
            "billing",
            "marketplace",
            "mcp_client",
            "org_tools",
            "scheduling",
            "teardrop",
            "tools",
            "tests",
            "docs",
            "README.md",
            "spec",
        ),
        focus=(
            "Identify concrete, incremental improvements to existing Teardrop products and workflows. "
            "Prioritize correctness, reliability, security, performance, developer experience, user value, "
            "and narrowly scoped product expansions. Ground every candidate in current code, tests, docs, "
            "and downstream spec compatibility; separate shipped behavior from proposals and assumptions."
        ),
        required_headings=IMPROVEMENT_REPORT_HEADINGS,
        report_guidance=(
            "For each candidate include: priority, confidence, user or business value, current behavior, "
            "exact repository paths and symbols, proposed incremental change, affected API/schema or SDK "
            "surface, effort, risk, dependencies, security and OWASP considerations, financial or ledger "
            "implications when applicable, focused tests, observability, rollout, and rollback. Do not propose "
            "new dependencies unless the benefit and maintenance cost are justified."
        ),
    ),
    "competitive": TopicConfig(
        label="Competitive research",
        directory="competitive",
        report_source="web",
        default_scopes=(),
        focus=(
            "Compare current primary sources, repository evidence, and dated public material. "
            "Name the comparison dimensions, distinguish capabilities from positioning, and avoid unsupported claims."
        ),
    ),
}


class ResearchToolError(RuntimeError):
    """Raised when source collection or the isolated research worker fails."""


def slugify(value: str, *, max_length: int = 80) -> str:
    """Return a stable, filesystem-safe slug for a report filename."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "research"


def normalize_query(value: str) -> str:
    """Normalize and bound the single question sent to the research worker."""
    query = " ".join(value.split())
    if not query:
        raise ValueError("--query must not be empty.")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"--query must be one focused question of at most {MAX_QUERY_CHARS} characters.")
    word_count = len(query.split())
    if word_count > MAX_QUERY_WORDS:
        raise ValueError(f"--query must be one focused question of at most {MAX_QUERY_WORDS} words.")
    return query


def normalize_required_paths(repo_root: Path, paths: Sequence[str]) -> tuple[str, ...]:
    """Normalize required evidence paths and reject paths outside the repository."""
    normalized: list[str] = []
    for raw_path in paths:
        path = _ensure_inside(repo_root / raw_path, repo_root)
        if not path.exists():
            raise ResearchToolError(f"Required evidence path does not exist: {raw_path}")
        relative_path = path.relative_to(repo_root).as_posix().rstrip("/")
        if relative_path not in normalized:
            normalized.append(relative_path)
    return tuple(normalized)


def missing_required_paths(required_paths: Sequence[str], source_paths: Sequence[str]) -> list[str]:
    """Return required paths that are not represented by collected sources."""
    return [
        required
        for required in required_paths
        if not any(path == required or path.startswith(f"{required}/") for path in source_paths)
    ]


def build_dry_run_payload(
    *,
    revision: str,
    evidence_dirty: bool,
    manifest_sha256: str | None,
    redaction_count: int,
    report_source: str,
    source_paths: Sequence[str],
    required_paths: Sequence[str],
) -> dict[str, object]:
    """Build stable machine-readable dry-run metadata."""
    return {
        "revision": revision,
        "evidence_dirty": evidence_dirty,
        "source_manifest_sha256": manifest_sha256,
        "source_redaction_count": redaction_count,
        "report_source": report_source,
        "source_file_count": len(source_paths),
        "source_files": list(source_paths),
        "required_paths": list(required_paths),
    }


def git_revision(repo_root: Path) -> str:
    """Return the repository revision used as the research evidence boundary."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def git_worktree_dirty(repo_root: Path) -> bool:
    """Return whether tracked or untracked working-tree content differs from HEAD."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ResearchToolError("Could not determine repository working-tree state.") from exc
    return bool(result.stdout.strip())


def _ensure_inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ResearchToolError(f"Path must stay inside the repository: {path}") from exc
    return resolved


def _is_allowed_source(path: Path, relative_path: Path) -> bool:
    parts = {part.lower() for part in relative_path.parts}
    name = path.name.lower()
    if parts & (IGNORED_DIRECTORIES | SENSITIVE_DIRECTORY_NAMES):
        return False
    if name in SENSITIVE_FILE_NAMES or name.startswith(".env"):
        return False
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return False
    normalized = relative_path.as_posix().lower()
    if normalized.startswith("docs/research/") and normalized != "docs/research/knowledge-index.md":
        return False
    return path.suffix.lower() in ALLOWED_SUFFIXES or name in {"readme", "license", "notice"}


def collect_source_documents(
    repo_root: Path,
    scopes: Sequence[str],
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_source_bytes: int = MAX_SOURCE_BYTES,
) -> list[tuple[str, str]]:
    """Collect deterministic, text-only source documents without sensitive paths."""
    root = repo_root.resolve()
    documents: list[tuple[str, str]] = []
    seen: set[Path] = set()
    total_bytes = 0

    for scope in scopes:
        candidate = _ensure_inside(root / scope, root)
        if not candidate.exists():
            raise ResearchToolError(f"Research scope does not exist: {scope}")
        candidates = [candidate] if candidate.is_file() else sorted(candidate.rglob("*"))
        for path in candidates:
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            relative_path = resolved.relative_to(root)
            if not _is_allowed_source(resolved, relative_path):
                continue
            try:
                size = resolved.stat().st_size
                if size > max_file_bytes or total_bytes + size > max_source_bytes:
                    continue
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            documents.append((relative_path.as_posix(), _redact_sensitive_text(content)))
            total_bytes += size
            if total_bytes >= max_source_bytes:
                return documents

    return documents


def collect_revision_documents(
    repo_root: Path,
    scopes: Sequence[str],
    revision: str,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_source_bytes: int = MAX_SOURCE_BYTES,
) -> list[tuple[str, str]]:
    """Collect source from an immutable Git tree rather than the working tree."""
    root = repo_root.resolve()
    normalized_scopes: list[str] = []
    for scope in scopes:
        candidate = _ensure_inside(root / scope, root)
        normalized_scopes.append(candidate.relative_to(root).as_posix().rstrip("/"))
    try:
        result = subprocess.run(
            ["git", "archive", "--format=tar", revision],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ResearchToolError(f"Could not read repository evidence at commit {revision}.") from exc

    documents: list[tuple[str, str]] = []
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            members = sorted((member for member in archive.getmembers() if member.isfile()), key=lambda item: item.name)
            for member in members:
                relative_path = Path(member.name)
                normalized_path = relative_path.as_posix()
                if not any(normalized_path == scope or normalized_path.startswith(f"{scope}/") for scope in normalized_scopes):
                    continue
                if not _is_allowed_source(relative_path, relative_path):
                    continue
                if member.size > max_file_bytes or total_bytes + member.size > max_source_bytes:
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                try:
                    content = extracted.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue
                documents.append((normalized_path, _redact_sensitive_text(content)))
                total_bytes += member.size
                if total_bytes >= max_source_bytes:
                    return documents
    except tarfile.TarError as exc:
        raise ResearchToolError(f"Git returned invalid evidence for commit {revision}.") from exc
    return documents


def _language_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".md": "markdown",
        ".rst": "text",
        ".sql": "sql",
        ".json": "json",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".css": "css",
    }.get(suffix, "text")


def render_source_manifest(documents: Sequence[tuple[str, str]], revision: str) -> str:
    """Render local source into a retriever-friendly Markdown document."""
    sections = [
        "# Teardrop repository source manifest",
        "",
        f"Evidence revision: `{revision}`",
        "",
        "Each section preserves the repository-relative path used for evidence citations.",
        "",
    ]
    for path, content in documents:
        safe_content = content.replace("```", "'''")
        sections.extend([f"## `{path}`", "", f"```{_language_for(path)}", safe_content, "```", ""])
    return "\n".join(sections)


def source_manifest_sha256(documents: Sequence[tuple[str, str]], revision: str) -> str | None:
    """Hash the exact sanitized local payload supplied to the research worker."""
    if not documents:
        return None
    manifest = render_source_manifest(documents, revision).encode("utf-8")
    return hashlib.sha256(manifest).hexdigest()


def source_redaction_count(documents: Sequence[tuple[str, str]]) -> int:
    """Count sanitization markers included in the exact worker payload."""
    markers = ("<redacted>", "<redacted-key-material>", "<redacted-provider-token>")
    return sum(content.count(marker) for _, content in documents for marker in markers)


def build_research_prompt(topic: str, query: str, revision: str, source_paths: Sequence[str]) -> str:
    """Build a topic-specific report contract for GPT-Researcher."""
    config = TOPICS[topic]
    local_context = (
        "The attached local source manifest is authoritative for repository behavior. Cite exact "
        "repository-relative paths and symbols from it."
        if source_paths
        else "There is no local source manifest; rely on dated external sources and identify that limitation."
    )
    local_citation_contract = (
        "Local citation contract: the final `## Sources` section MUST contain at least one Markdown bullet "
        "in this exact shape, using a path from the manifest: - `README.md` - product capability evidence. "
        "Use manifest paths such as `README.md` or `docs/architecture.md`; do not cite `repo-source.md`, "
        "which is only the transport file. A high-level roadmap analysis still requires at least one relevant "
        "local path, and inline citations alone do not satisfy this contract."
        if source_paths
        else "No local citation contract applies because no repository source files were collected."
    )
    report_guidance = config.report_guidance or (
        "For each finding include: severity or priority, confidence (high/medium/low), claim, evidence "
        "path/symbol or URL, impact, and why the evidence supports the claim. For security findings, "
        "classify each as `verified`, `inconclusive`, or `not reproduced`."
    )
    required_headings = "\n".join(config.required_headings)
    return f"""You are researching Teardrop at commit {revision}.

Research question:
{query}

Scope and focus:
{config.focus}

Evidence rules:
- {local_context}
- {local_citation_contract}
- Treat live code and primary documentation as stronger evidence than repository notes or marketing claims.
- Distinguish verified facts, likely risks, unresolved hypotheses, and recommendations.
- Do not invent a vulnerability. A suspected gap must include the attack precondition, affected path,
  and the exact evidence needed to verify it.
- Do not include secrets, credentials, private keys, or copied sensitive values in the report.

Use exactly these sections:
{required_headings}

{report_guidance}"""


def _redact_sensitive_text(text: str) -> str:
    pem_pattern = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL)
    assignment_pattern = re.compile(r"(?im)^([ \t]*(?:export[ \t]+)?[\"']?)([A-Z0-9_-]+)([\"']?[ \t]*[=:][ \t]*)([^\r\n]+)$")
    provider_token_pattern = re.compile(
        r"(?i)\b(?:sk-(?:proj-)?[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}|github_pat_[a-z0-9_]{16,})"
    )
    bearer_pattern = re.compile(r"(?i)(\bBearer[ \t]+)[a-z0-9._~-]{16,}")
    redacted = pem_pattern.sub("<redacted-key-material>", text)

    def redact_assignment(match: re.Match[str]) -> str:
        identifier = match.group(2)
        segments = [segment for segment in re.split(r"[_-]+", identifier.upper()) if segment]
        token_prefixes = {"ACCESS", "API", "AUTH", "BEARER", "GITHUB", "ID", "OPENAI", "REFRESH", "STRIPE"}
        sensitive = (
            "PASSWORD" in segments
            or "PASSWD" in segments
            or "SECRET" in segments
            or ("PRIVATE" in segments and "KEY" in segments)
            or ("API" in segments and "KEY" in segments)
            or identifier.upper() == "TOKEN"
            or (segments[-1:] == ["TOKEN"] and bool(set(segments[:-1]) & token_prefixes))
        )
        value = match.group(4).strip()
        quoted_literal = bool(re.fullmatch(r"([\"']).*\1,?", value))
        token_like_scalar = bool(re.fullmatch(r"[a-z0-9._~+/=-]{8,},?", value, re.IGNORECASE))
        environment_style = identifier == identifier.upper()
        if not sensitive or not (quoted_literal or token_like_scalar or environment_style):
            return match.group(0)
        return f"{match.group(1)}{identifier}{match.group(3)}<redacted>"

    redacted = assignment_pattern.sub(redact_assignment, redacted)
    redacted = provider_token_pattern.sub("<redacted-provider-token>", redacted)
    return bearer_pattern.sub(r"\1<redacted>", redacted)


def validate_research_report(
    report: str,
    source_paths: Sequence[str],
    required_headings: Sequence[str] = REQUIRED_REPORT_HEADINGS,
) -> None:
    """Reject malformed output before it enters the durable research archive."""
    heading_matches = [re.search(rf"(?m)^{re.escape(heading)}[ \t]*$", report) for heading in required_headings]
    missing = [heading for heading, match in zip(required_headings, heading_matches, strict=True) if match is None]
    if missing:
        raise ResearchToolError(f"Research report is missing required sections: {', '.join(missing)}")
    positions = [match.start() for match in heading_matches if match is not None]
    if positions != sorted(positions):
        raise ResearchToolError("Research report sections are out of order.")
    sources_match = heading_matches[-1]
    assert sources_match is not None
    sources = report[sources_match.end() :].strip()
    if not sources:
        raise ResearchToolError("Research report has an empty Sources section.")
    if source_paths and not any(
        re.search(rf"(?m)^[ \t]*-[ \t]+`{re.escape(path)}`(?:[ \t]|$)", sources) for path in source_paths
    ):
        raise ResearchToolError(
            "Research report does not cite any collected repository path in Sources; "
            "expected a bullet such as '- `README.md` - evidence.'"
        )


def resolve_researcher_python(requested: str | None) -> Path:
    """Locate the isolated interpreter used for GPT-Researcher."""
    configured = requested or os.getenv("GPT_RESEARCHER_PYTHON")
    if configured:
        interpreter = Path(configured).expanduser().resolve()
    else:
        executable = "python.exe" if os.name == "nt" else "python"
        interpreter = (REPO_ROOT / ".research-venv" / ("Scripts" if os.name == "nt" else "bin") / executable).resolve()
    if not interpreter.is_file():
        raise ResearchToolError(
            f"Research interpreter not found: {interpreter}. Create it with "
            "`.venv\\Scripts\\python -m venv .research-venv` and install `requirements-research.txt`."
        )
    return interpreter


async def conduct_research(
    prompt: str,
    *,
    report_source: str,
    documents: Sequence[tuple[str, str]],
    revision: str,
    researcher_python: Path,
    github_mcp: bool,
    timeout_seconds: float,
    max_subtopics: int,
    required_headings: Sequence[str] = REQUIRED_REPORT_HEADINGS,
) -> str:
    """Run the isolated worker with a temporary, sanitized source manifest."""
    with tempfile.TemporaryDirectory(prefix="teardrop-research-") as temporary_directory:
        source_directory = Path(temporary_directory)
        document_directory = source_directory / "documents"
        if documents:
            document_directory.mkdir()
            (document_directory / "repo-source.md").write_text(render_source_manifest(documents, revision), encoding="utf-8")
        request_path = source_directory / "request.json"
        report_path = source_directory / "report.md"
        request_path.write_text(
            json.dumps(
                {
                    "prompt": prompt,
                    "report_source": report_source,
                    "doc_path": str(document_directory) if documents else None,
                    "github_mcp": github_mcp,
                    "max_subtopics": max_subtopics,
                }
            ),
            encoding="utf-8",
        )
        process = await asyncio.create_subprocess_exec(
            str(researcher_python),
            "-m",
            "scripts.research_repo_worker",
            "--request",
            str(request_path),
            "--output",
            str(report_path),
            cwd=str(REPO_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ResearchToolError(f"Research worker timed out after {timeout_seconds:g} seconds.") from exc
        if process.returncode != 0:
            details = _redact_sensitive_text(stderr.decode("utf-8", errors="replace").strip())
            raise ResearchToolError(details or f"Research worker exited with status {process.returncode}.")
        try:
            report = report_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ResearchToolError("Research worker did not produce a readable report.") from exc

    if not isinstance(report, str) or not report.strip():
        raise ResearchToolError("GPT-Researcher returned an empty report.")
    report = _redact_sensitive_text(report.strip())
    validate_research_report(report, [path for path, _ in documents], required_headings)
    return report


def render_report(
    *,
    topic: str,
    query: str,
    revision: str,
    report_source: str,
    evidence_dirty: bool,
    manifest_sha256: str | None,
    redaction_count: int,
    generated_at: datetime,
    source_paths: Sequence[str],
    required_paths: Sequence[str] = (),
    report: str,
) -> str:
    """Add durable metadata and the draft-review boundary to a report."""
    config = TOPICS[topic]
    source_lines = "\n".join(f"- `{path}`" for path in source_paths) or "- External sources only"
    return "\n".join(
        [
            "---",
            f"topic: {topic}",
            "status: draft",
            f"generated_at_utc: {json.dumps(generated_at.isoformat())}",
            f"evidence_commit: {json.dumps(revision)}",
            f"evidence_dirty: {json.dumps(evidence_dirty)}",
            f"source_manifest_sha256: {json.dumps(manifest_sha256)}",
            f"source_redaction_count: {redaction_count}",
            f"report_source: {json.dumps(report_source)}",
            f"prompt_version: {PROMPT_VERSION}",
            f"required_paths: {json.dumps(list(required_paths))}",
            f"query: {json.dumps(query)}",
            "---",
            "",
            f"# {config.label}",
            "",
            "> This report is unverified research. Do not treat a suspected security issue as confirmed until a human "
            "verifies it against the cited live code.",
            "",
            "## Research scope",
            "",
            source_lines,
            "",
            "## Report",
            "",
            _redact_sensitive_text(report),
            "",
        ]
    )


def _output_path(root: Path, topic: str, query: str, requested: str | None) -> Path:
    if requested:
        return _ensure_inside(root / requested, root)
    today = datetime.now(timezone.utc).date().isoformat()
    return RESEARCH_ROOT / TOPICS[topic].directory / f"{today}-{slugify(query)}.md"


def find_stale_reports(research_root: Path, current_revision: str) -> list[tuple[Path, str]]:
    """Find durable reports whose evidence cannot represent the current clean commit."""
    stale: list[tuple[Path, str]] = []
    for config in TOPICS.values():
        topic_directory = research_root / config.directory
        if not topic_directory.is_dir():
            continue
        for report_path in sorted(topic_directory.rglob("*.md")):
            try:
                prefix = report_path.read_text(encoding="utf-8").split("---", maxsplit=2)[1]
            except (OSError, UnicodeDecodeError, IndexError):
                stale.append((report_path, "missing or malformed front matter"))
                continue
            metadata: dict[str, object] = {}
            for line in prefix.splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                try:
                    metadata[key.strip()] = json.loads(value.strip())
                except json.JSONDecodeError:
                    metadata[key.strip()] = value.strip()
            if metadata.get("status") == "superseded":
                continue
            evidence_commit = metadata.get("evidence_commit")
            if not isinstance(evidence_commit, str):
                stale.append((report_path, "missing evidence_commit"))
            elif evidence_commit != current_revision:
                stale.append((report_path, f"evidence commit {evidence_commit} differs from {current_revision}"))
            elif metadata.get("evidence_dirty") is not False:
                stale.append((report_path, "evidence was dirty or lacks clean-tree provenance"))
    return stale


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", choices=sorted(TOPICS))
    parser.add_argument(
        "--query",
        help=f"One focused research question ({MAX_QUERY_CHARS} characters and {MAX_QUERY_WORDS} words maximum).",
    )
    parser.add_argument("--scope", action="append", help="Repository-relative source scope; repeat for multiple paths.")
    parser.add_argument(
        "--require-path",
        action="append",
        default=[],
        help="Require a repository file or directory to appear in collected evidence; repeat for dependencies.",
    )
    parser.add_argument("--report-source", choices=("local", "web", "hybrid"), help="Override the topic default.")
    parser.add_argument("--github-mcp", action="store_true", help="Enable the GitHub MCP retriever; requires GITHUB_TOKEN.")
    parser.add_argument("--researcher-python", help="Path to the isolated GPT-Researcher Python interpreter.")
    parser.add_argument("--output", help="Repository-relative output path; defaults under docs/research/<topic>.")
    parser.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    parser.add_argument("--max-source-bytes", type=int, default=MAX_SOURCE_BYTES)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-subtopics", type=int, default=DEFAULT_MAX_SUBTOPICS)
    parser.add_argument("--force", action="store_true", help="Overwrite an existing draft report.")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow a research run against uncommitted content; the report records evidence_dirty: true.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and list local source files without calling GPT-Researcher.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON; only valid with --dry-run.")
    parser.add_argument(
        "--check-stale",
        action="store_true",
        help="Exit nonzero when archived reports do not match the current clean evidence commit.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check_stale:
        revision = git_revision(REPO_ROOT)
        if revision == "unknown":
            print("Stale check failed: current Git revision is unknown.", file=sys.stderr)
            return 1
        stale_reports = find_stale_reports(RESEARCH_ROOT, revision)
        if stale_reports:
            print(f"stale research reports: {len(stale_reports)}")
            for path, reason in stale_reports:
                print(f"- {path.relative_to(REPO_ROOT)}: {reason}")
            return 1
        print("research reports are current")
        return 0

    if not args.topic or args.query is None:
        print("--topic and --query are required unless --check-stale is used.", file=sys.stderr)
        return 2
    try:
        query = normalize_query(args.query)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.timeout_seconds <= 0:
        print("--timeout-seconds must be greater than zero.", file=sys.stderr)
        return 2
    if not 1 <= args.max_subtopics <= 10:
        print("--max-subtopics must be between 1 and 10.", file=sys.stderr)
        return 2
    if args.json and not args.dry_run:
        print("--json requires --dry-run.", file=sys.stderr)
        return 2

    config = TOPICS[args.topic]
    report_source = args.report_source or config.report_source
    scopes = args.scope or config.default_scopes
    revision = git_revision(REPO_ROOT)
    if revision == "unknown":
        print("Research failed: a Git commit is required for evidence provenance.", file=sys.stderr)
        return 1
    try:
        required_paths = normalize_required_paths(REPO_ROOT, args.require_path)
        if required_paths and report_source not in {"local", "hybrid"}:
            raise ResearchToolError("--require-path requires local or hybrid report sources.")
        evidence_dirty = git_worktree_dirty(REPO_ROOT)
        collector = collect_source_documents if evidence_dirty else collect_revision_documents
        documents = (
            (
                collector(
                    REPO_ROOT,
                    scopes,
                    **({} if evidence_dirty else {"revision": revision}),
                    max_file_bytes=args.max_file_bytes,
                    max_source_bytes=args.max_source_bytes,
                )
            )
            if report_source in {"local", "hybrid"}
            else []
        )
    except ResearchToolError as exc:
        print(f"Research failed: {exc}", file=sys.stderr)
        return 1
    if report_source in {"local", "hybrid"} and not documents:
        print("No eligible local source files were collected.", file=sys.stderr)
        return 2
    if args.github_mcp and report_source == "local":
        print("--github-mcp requires --report-source web or hybrid.", file=sys.stderr)
        return 2

    source_paths = [path for path, _ in documents]
    manifest_sha256 = source_manifest_sha256(documents, revision)
    redaction_count = source_redaction_count(documents)
    missing_paths = missing_required_paths(required_paths, source_paths)
    if missing_paths:
        print(
            "Research failed: required evidence paths were not collected: " + ", ".join(missing_paths),
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        if args.json:
            print(
                json.dumps(
                    build_dry_run_payload(
                        revision=revision,
                        evidence_dirty=evidence_dirty,
                        manifest_sha256=manifest_sha256,
                        redaction_count=redaction_count,
                        report_source=report_source,
                        source_paths=source_paths,
                        required_paths=required_paths,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        print(f"revision: {revision}")
        print(f"evidence_dirty: {str(evidence_dirty).lower()}")
        print(f"source_manifest_sha256: {manifest_sha256 or 'none'}")
        print(f"source_redaction_count: {redaction_count}")
        print(f"report_source: {report_source}")
        print(f"source_files: {len(source_paths)}")
        for path in source_paths:
            print(f"- {path}")
        return 0

    if evidence_dirty and not args.allow_dirty:
        print("Research failed: working tree is dirty; commit changes or pass --allow-dirty.", file=sys.stderr)
        return 1

    try:
        output_path = _output_path(REPO_ROOT, args.topic, query, args.output)
    except ResearchToolError as exc:
        print(f"Research failed: {exc}", file=sys.stderr)
        return 1
    if output_path.exists() and not args.force:
        print(f"Refusing to overwrite existing report: {output_path}. Use --force.", file=sys.stderr)
        return 2

    try:
        researcher_python = resolve_researcher_python(args.researcher_python)
        prompt = build_research_prompt(args.topic, query, revision, source_paths)
        report = asyncio.run(
            conduct_research(
                prompt,
                report_source=report_source,
                documents=documents,
                revision=revision,
                researcher_python=researcher_python,
                github_mcp=args.github_mcp,
                timeout_seconds=args.timeout_seconds,
                max_subtopics=args.max_subtopics,
                required_headings=config.required_headings,
            )
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            render_report(
                topic=args.topic,
                query=query,
                revision=revision,
                report_source=report_source,
                evidence_dirty=evidence_dirty,
                manifest_sha256=manifest_sha256,
                redaction_count=redaction_count,
                generated_at=datetime.now(timezone.utc),
                source_paths=source_paths,
                required_paths=required_paths,
                report=report,
            ),
            encoding="utf-8",
        )
    except (ResearchToolError, OSError) as exc:
        print(f"Research failed: {exc}", file=sys.stderr)
        return 1

    print(f"wrote draft report: {output_path.relative_to(REPO_ROOT)}")
    print("Review the cited live code, then update knowledge-index.md only after verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
