# Teardrop Agent Instructions

Token-efficient workflow for coding agents on this repo. See [CONTRIBUTING.md](../CONTRIBUTING.md) for setup, build, and lint commands.

## Context Retrieval
- Check `/memories/repo/` first (architecture, marketplace access modes, testing gotchas, etc.) before exploring the codebase from scratch.
- Check `docs/research/knowledge-index.md` and the linked current report before planning security, roadmap, or competitive work. Treat `draft`, `inconclusive`, and `superseded` reports as non-authoritative.
- For security claims, verify the cited live code and symbols against `.github/skills/teardrop-domain-invariants/SKILL.md`; repository research is evidence to inspect, not a substitute for tests or review.
- Prefer targeted `read_file` line ranges over reading entire large modules (e.g. `agent/nodes.py`, `teardrop/app.py`).
- Scope greps with an `includePattern` (e.g. `agent/**`, `billing/**`) instead of searching the whole workspace.

## Editing
- Make multi-file changes incrementally per module; verify before moving to the next.
- Only show changed lines in explanations, not unchanged boilerplate.
