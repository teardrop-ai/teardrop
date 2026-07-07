# Teardrop Agent Instructions

Token-efficient workflow for coding agents on this repo. See [CONTRIBUTING.md](../CONTRIBUTING.md) for setup, build, and lint commands.

## Context Retrieval
- Check `/memories/repo/` first (architecture, marketplace access modes, testing gotchas, etc.) before exploring the codebase from scratch.
- Prefer targeted `read_file` line ranges over reading entire large modules (e.g. `agent/nodes.py`, `teardrop/app.py`).
- Scope greps with an `includePattern` (e.g. `agent/**`, `billing/**`) instead of searching the whole workspace.

## Editing
- Make multi-file changes incrementally per module; verify before moving to the next.
- Only show changed lines in explanations, not unchanged boilerplate.
