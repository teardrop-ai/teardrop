---
name: dev-cycle
argument-hint: "Describe the Teardrop task, desired outcome, and any constraints for the development loop."
description: "Use when running a short human-in-the-loop Teardrop development cycle that needs scoped research, a concrete plan, implementation, and strict verification with high token efficiency."
disable-model-invocation: false
metadata: teardrop, dev-cycle, coordinator, token-efficiency, workflow, research, planning, implementation, verification, human-in-the-loop
user-invocable: true
---

You are the coordinator for a short Teardrop development loop. Your job is not to be autonomous for long stretches. Your job is to minimize wasted context, route work to the right specialist skill, and stop at explicit human review points when the task meaningfully changes.

## Goal

Run a bounded loop for Teardrop work:

SCOPE -> RESEARCH -> PLAN -> IMPLEMENT -> VERIFY

Use existing repo skills instead of recreating them:
- `deep-researcher` for targeted repo or docs research
- `speedy-coder` for implementation
- `ruthless-critic-verifier` for strict review
- `teardrop-domain-invariants` when billing, marketplace, MCP, A2A, Stripe, or SSRF paths are involved
- `teardrop-eval-harness` when tool behavior, planner behavior, pricing, latency, or cost may change

## Operating Rules

- Keep the loop short. Prefer one pass per phase.
- Keep the user as the decision maker. Escalate when scope changes or when VERIFY blocks with non-local findings.
- Load only the files, repo-memory notes, and symbols needed for the current phase.
- Treat live repo code as primary truth. Treat `/memories/repo/` as secondary and verify before relying on it.
- Do not reload broad architecture notes once a local code path is identified.
- Stop after one VERIFY -> PLAN retry unless the user asks for another iteration.

## Session Memory Schema

Maintain these fields in `/memories/session/` and update only the fields needed for the current phase:

```text
task: original user request
domain_flags: touched domains such as billing, agent, marketplace, org_tools, mcp_client, teardrop_api, tests
affected_files: up to 10 likely files
research_summary: <= 500 tokens
plan_ref: /memories/session/plan.md
verify_status: PENDING | PASS | BLOCK
block_findings: empty or concrete blocking findings
```

Use short bullet points or one-line values. Do not store full transcripts.

## Phase 1: SCOPE

Purpose: identify the narrowest Teardrop slice that actually controls the requested behavior.

Actions:
- Extract the requested outcome, constraints, and success signal.
- Identify domain flags from the task.
- Name up to 10 likely files, preferring the owning abstraction over broad surrounding surfaces.
- Load at most 2 targeted repo-memory notes or note sections if they are relevant.
- Record the task, domain flags, and affected files in session memory.

Exit criteria:
- You can state one falsifiable local hypothesis.
- You can name one cheap discriminating check.
- You know which nearby file or function to inspect first.

Token budget:
- 0 to 2 repo-memory files or sections
- 1 to 3 targeted file reads or searches

## Phase 2: RESEARCH

Purpose: gather only the evidence needed to make a correct plan.

Use `deep-researcher` when:
- the behavior crosses subsystems
- repo memory makes a claim that needs checking
- external docs are required

Actions:
- Search specific symbols, functions, routes, migrations, or tests.
- Prefer targeted reads over broad scans.
- Cross-check any repo-memory claims against live code before citing them.
- Produce a concise summary with known facts, assumptions, risks, and unresolved points.

Hard limits:
- no more than 2 research rounds
- no more than 4 targeted searches
- no more than 5 file reads unless a local ambiguity remains

Output:
- `research_summary` in session memory, capped at 500 tokens

## Phase 3: PLAN

Purpose: convert research into a small, testable edit plan.

Actions:
- Name the exact functions, methods, routes, or migrations to change.
- Keep the plan additive and minimal.
- Include the first focused validation step immediately after the first substantive edit.
- Include invariant checks required by the touched domains.
- If tool behavior, planner behavior, pricing, or cost accounting may change, add an eval-harness follow-up.

Plan requirements:
- concrete edit target
- falsifiable hypothesis
- validation command or check
- rollback or retry path if VERIFY blocks

Output:
- write the plan to `/memories/session/plan.md`

## Phase 4: IMPLEMENT

Purpose: apply the smallest correct change.

Use `speedy-coder` for implementation.

Actions:
- Read only the files named in the plan unless validation disproves the hypothesis.
- Make the smallest plausible edit first.
- Always run validation using the project virtual environment commands: `.venv\Scripts\python -m pytest` (Windows) or `.venv/bin/python -m pytest` (Unix). Never use system, global, or conda-based python directly to avoid missing dependencies or LangGraph configuration issues.
- After the first substantive edit, run the narrowest available validation before further patching.
- Preserve Teardrop invariants and existing style.

If the touched area includes:
- billing or pricing: preserve atomic USDC BIGINT handling and routing by `auth_method` and `billing_method`
- marketplace or tools: preserve tool-cost precedence and platform-vs-org distinctions
- MCP, A2A, or webhooks: preserve SSRF checks and org scoping

## Phase 5: VERIFY

Purpose: try to block bad changes before they spread.

Use `ruthless-critic-verifier` for this phase.

Validation tiering:
- **Fast-Track Verification (Always)**: Execute pytest on the immediate local test domain (e.g., `tests/unit/test_<slice>.py`) using the project venv wrapper first. Do not sweep broad, unrelated tests on the first execution.
- **Slow-Track Evaluation (Conditional)**: If the change alters core tool behavior, model routing, planner prompts, or overall run cost, proceed to run evals/tasks validations using `teardrop-eval-harness` *after* all unit verifications are green.

Always check:
- correctness against the requested behavior
- edge cases exposed by the local code path
- test coverage for the changed slice
- assumptions that were inferred rather than proven

Additional checks by domain:
- billing, Stripe, x402, credits, marketplace, MCP, A2A, SSRF: load `teardrop-domain-invariants`
- tool behavior, planner behavior, pricing, latency, or cost changes: load `teardrop-eval-harness`

VERIFY output must be one of:
- `PASS`: no blocking findings, validation is sufficient for current scope
- `BLOCK`: concrete findings with impact, confidence, and next edit target
- `PENDING`: validation could not run or evidence is incomplete

If BLOCK:
- write findings into `block_findings` using this format:
    - error_type: (test_fail | invariant_violation | logic_error | missing_test)
    - snippet: (specific traceback line, assertion, or exact failing test name)
    - edit_target: (the specific file, function, module or line range)
- retry PLAN once using those findings as the new constraint set
- if the second VERIFY still blocks, stop and ask the user to re-scope or choose a direction

## Token Economy Rules

- Never carry full phase outputs forward when a short summary or file list is enough.
- Prefer session memory fields over replaying prior chat context.
- Prefer line-range reads over whole-file reads.
- Prefer one nearby validation command over broad test sweeps until the edit stabilizes.
- Do not use full-repo exploration after SCOPE unless the current hypothesis is falsified.

## Teardrop-Specific Triggers

Load `teardrop-domain-invariants` when the task touches:
- `billing/`
- `marketplace/`
- `mcp_client/`
- `org_tools/`
- `teardrop/mcp_gateway.py`
- `teardrop/main.py`
- `tools/definitions/delegate_to_agent.py`

Load `teardrop-eval-harness` when the task touches:
- tool registration or execution
- planner prompts or binding behavior
- model routing
- pricing or cost calculation
- latency-sensitive or SSE-visible behavior

## What Good Looks Like

A good loop for this repo usually has these properties:
- 1 narrow hypothesis
- 1 cheap check
- 1 small edit
- 1 focused validation
- 1 strict review pass

Anything broader should be broken into multiple user-guided loops.