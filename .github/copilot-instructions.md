# Teardrop Agent Instructions

Token-efficient workflow for coding agents on this repo. See [CONTRIBUTING.md](../CONTRIBUTING.md) for setup, build, and lint commands.

## Context Retrieval
- Check `/memories/repo/` first (architecture, marketplace access modes, testing gotchas, etc.) before exploring the codebase from scratch.
- Check `docs/research/knowledge-index.md` and the linked current report before planning security, roadmap, or competitive work. Treat `draft`, `inconclusive`, and `superseded` reports as non-authoritative.
- For security claims, verify the cited live code and symbols against `.github/skills/teardrop-domain-invariants/SKILL.md`; repository research is evidence to inspect, not a substitute for tests or review.
- Prefer targeted `read_file` line ranges over reading entire large modules (e.g. `agent/nodes.py`, `teardrop/app.py`).
- Scope greps with an `includePattern` (e.g. `agent/**`, `billing/**`) instead of searching the whole workspace.

## Durable Research Routing
- Use normal search, reads, tests, and focused implementation work for narrow bug diagnosis, failing-test investigation, code explanations, and single-symbol verification.
- Invoke `.github/skills/repo-research/SKILL.md` only when the question is unresolved in the verified research index, needs durable multi-system/revision/primary-source synthesis, and has a named future consumer, expected shelf life, and decision it can affect. Record those three values before invocation.
- Do not create a report merely because a question is broad or interesting. The expected reuse must justify provider cost and human review; otherwise answer it through ordinary coding-agent pathways.
- When several qualifying questions are independent, delegate at most three to the coordinator-only `repo-researcher` agent in parallel. Deduplicate normalized questions and keep dependent questions sequential.
- Run shared interpreter and presence-only credential checks once, but require each subagent to run its own dry-run. Pass `--expect-revision`, `--expect-manifest-sha256`, and `--expect-evidence-dirty` from that dry-run to the final command.
- Treat only selected eligible dirty paths as evidence-dirty. Unrelated worktree edits and generated `docs/research/<topic>/*.md` drafts do not invalidate immutable `HEAD` evidence. Selected dirty source requires explicit `--allow-dirty`, remains unpromotable, and must never trigger stash/reset/commit automation.
- Preserve successful drafts on partial failure. Never retry paid research automatically, use `--force` implicitly, or update `docs/research/knowledge-index.md` without human verification.
- Calibrate research consumption: treat `docs/research/**` drafts as hypotheses plus a citation map, not facts. Verify every cited file/symbol against live code and check `evidence_commit`/staleness before relying on a claim. Only `knowledge-index.md` and promoted reports are trusted without re-verification.

## Editing
- Make multi-file changes incrementally per module; verify before moving to the next.
- Only show changed lines in explanations, not unchanged boilerplate.

## Testing
- For broad validation of the mocked suites, prefer `.venv\Scripts\python -m pytest tests/unit/ tests/api/ -n auto --dist load` (or `.venv/bin/python -m pytest ...` on Unix) for faster feedback.
- Do **not** use `-n auto` on `tests/integration` — its session-scoped Postgres container uses a fixed port and truncates shared tables. Run integration serially with `-n 0`.
