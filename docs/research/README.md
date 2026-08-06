# Repository Research Hub

This directory is the Git-tracked, developer-only knowledge base for research that helps coding agents understand Teardrop. It is separate from the product's Neon `org_memories`, runtime `scheduling/`, and VS Code's `/memories/repo/` session memory.

## Topics

- `security/` — billing, settlement, auth, SSRF, marketplace, MCP/A2A, and migration exploit analysis.
- `roadmap/` — evidence-backed product opportunities and dependencies.
- `improvements/` — code-grounded incremental product improvements and narrowly scoped expansions for coding agents.
- `competitive/` — dated comparisons with external products and open-source projects.
- `knowledge-index.md` — entry point for coding agents; list only current, verified findings here.

## Workflow

1. Create the isolated research environment once. It must not be installed into Teardrop's shared `.venv` because GPT-Researcher has incompatible MCP dependencies:

  ```powershell
  py -3.12 -m venv .research-venv
  .research-venv\Scripts\python -m pip install -r requirements-research.txt
  ```

2. Set the provider credentials required by the selected research source in the shell that launches the command. Web and hybrid research normally require the configured LLM and search-provider keys; GitHub MCP additionally requires `GITHUB_TOKEN`.
3. Pin the repository revision and run `.venv\Scripts\python -m scripts.research_repo` with one focused question.
4. Treat the generated report as `draft`. GPT output is a research lead, not a security verdict.
5. Verify the report's `evidence_commit`, `source_manifest_sha256`, and cited live files or symbols. For security, reproduce the behavior or add a focused test where practical.
6. Update the report status and `knowledge-index.md` only after human review. Mark outdated entries `superseded`; do not silently rewrite history.
7. Ask coding agents to read the index and the relevant report before planning changes.

## When Agents Create Reports

Agents use ordinary repository search, focused reads, tests, and implementation work for narrow bug diagnosis,
failing-test investigation, code explanations, and single-symbol verification. They invoke GPT-Researcher only
when the question is unresolved in the verified index, needs durable synthesis across subsystems, revisions, or
primary external sources, and has all of the following recorded before invocation:

- a named future consumer;
- an expected shelf life longer than the current task;
- a future implementation, architecture, verification, roadmap, provider, or risk decision the report can affect;
- expected reuse that justifies provider cost and human review.

Broad or interesting questions without a concrete future consumer stay in the ordinary coding-agent workflow.
Independent qualifying questions may be delegated to at most three coordinator-only research subagents. Dependent
questions are run sequentially. A partial fan-out preserves successful drafts and reports failed questions without
automatically retrying paid calls.

For improvement research, the coding agent applies the ruthless-critic review contract to each candidate, verifies the cited live code, and adds or runs focused tests before a human promotes it. After implementation, commit the change and rerun the same or a new focused question against the new revision; this is the recursive roadmap loop.

Example:

```powershell
.venv\Scripts\python -m scripts.research_repo `
  --topic security `
  --query "Can an untrusted caller bypass billing or settlement controls?"
```

Use `--dry-run` to inspect the local source manifest without calling an LLM. Use `--github-mcp` for competitive GitHub research when `GITHUB_TOKEN` is present. The runner excludes keys, environment files, virtual environments, and oversized/binary files, but generated reports still require review for accidental sensitive content.

The default worker interpreter is `.research-venv\Scripts\python` on Windows and `.research-venv/bin/python` on POSIX. Set `GPT_RESEARCHER_PYTHON` or pass `--researcher-python` when the environment is elsewhere. `--report-source hybrid` combines the sanitized local manifest with web research; `--github-mcp` is valid for `web` or `hybrid`, not `local`.

## Safety and Operations

- Real runs require a clean Git working tree by default. `--allow-dirty` is available for exploratory drafts, records `evidence_dirty: true`, and makes the report ineligible for promotion until rerun cleanly.
- Clean runs read source directly from the immutable Git commit rather than mutable working-tree files.
- Cleanliness is evidence-scoped: unrelated worktree edits and generated `docs/research/<topic>/*.md` drafts do not
  invalidate an otherwise clean selected scope. A modified selected source or `knowledge-index.md` is relevant dirty
  evidence and blocks provider execution until explicitly approved with `--allow-dirty`.
- Automatic runs bind the final provider call to the dry-run's revision, sanitized manifest hash, and evidence-dirty
  state. If any changes, the provider is not called.
- Non-force report creation uses exclusive file creation. A parallel collision fails closed and a failed write removes
  its partial claimed file; `--force` remains an explicit overwrite option.
- Local files are filtered and common credential assignments, private-key blocks, bearer credentials, and provider-token formats are redacted before upload. `source_redaction_count` records how many markers entered the manifest. This is defense in depth, not a substitute for repository secret scanning or reviewing provider retention terms.
- Generated reports under `docs/research/` are excluded from later source collection; only `knowledge-index.md` can re-enter the roadmap corpus.
- The worker defaults to a 30-minute timeout and five subtopics. Use `--timeout-seconds` and `--max-subtopics` to tighten runtime and cost exposure.
- Reports are rejected unless all required sections exist, Sources is non-empty, and local/hybrid output cites at least one collected repository path in Sources.
- Run `.venv\Scripts\python -m scripts.research_repo --check-stale` before relying on archived reports. This conservative check exits nonzero for commit drift, dirty evidence, or missing provenance; intentionally superseded reports are ignored.

## Evidence Contract

Every report records its UTC generation time, evidence commit and dirty state, exact sanitized-manifest hash, redaction count, prompt version, query, source paths, status, confidence, uncertainty, and citations. Security findings must state whether they are `verified`, `inconclusive`, or `not reproduced`; improvement candidates must state current behavior, proposed change, value, affected symbols, risk, dependencies, tests, rollout, and rollback. A suspected gap or unverified improvement must not be promoted as fact. Only clean-evidence reports may be promoted to the knowledge index.
