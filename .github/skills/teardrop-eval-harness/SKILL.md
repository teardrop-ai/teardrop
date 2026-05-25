---
name: teardrop-eval-harness
argument-hint: "Describe the change that may affect agent quality, tool selection, latency, or cost."
description: "Use after adding tools, changing planner behavior, adjusting pricing, or modifying model routing. Guides writing eval tasks, running baseline diffs, and enforcing regressions with Teardrop eval harness."
disable-model-invocation: false
metadata: teardrop, eval, evaluation, agent quality, tool selection, regression, latency, cost, baseline, scoring, tests, harness, planner, benchmark
user-invocable: true
---

You are an evaluation engineer for Teardrop's agent quality harness.

## When to Use This Skill
- Use when the change can alter tool choice, response quality, latency, cost, or SSE-visible behavior.
- Prefer unit/API tests for deterministic function logic and error handling.
- Prefer eval tasks for end-to-end agent behavior and tool-selection contracts.

## Eval Task Format (evals/runner.py EvalTask)
Use this shape in evals/tasks/*.yaml:

```yaml
- id: "suite.scenario.001"
  messages:
    - role: "user"
      content: "What is the USDC balance of vitalik.eth on Base?"
  expected_tool_calls: ["resolve_ens", "get_erc20_balance"]
  expected_text_contains: ["USDC", "vitalik"]
  expected_text_not_contains: ["error"]
  max_duration_ms: 8000
  max_cost_usdc: 500000
  scorer: "contains"
  rubric: "Resolves ENS and returns a USDC balance."
```

## Eval Authoring Rules
- Assert behavior, not implementation details.
- Use expected_tool_calls for key tool routing expectations.
- Use expected_text_contains for stable semantic checks.
- Avoid exact-value checks on live market data.
- Keep max_cost_usdc in atomic USDC and set realistic headroom.
- Add to an existing suite unless a truly new domain is required.

## Running the Harness
```powershell
python -m evals.cli --suite smoke --base-url http://localhost:8000 --token <jwt>
python -m evals.cli --suite smoke --base-url http://localhost:8000 --token <jwt> --output baseline.json
python -m evals.cli --suite smoke --base-url http://localhost:8000 --token <jwt> --baseline-report baseline.json --output candidate.json
```

## Regression Guardrails
- score_delta < -0.05 is a regression unless explicitly justified.
- cost_delta_pct > +20% is a regression unless tied to approved pricing/tool changes.
- latency_delta_pct > +30% is a regression unless expected for known-expensive tool flows.
- New tools should have at least one eval task validating tool invocation and output shape.

## Scoring Model Summary
- contains: fraction of expected substrings found
- contains_pattern: regex-based fraction
- exact: exact string equality
- not_contains: fraction of excluded substrings absent
- Task pass condition in runner: score >= 0.8 and tool/cost/duration checks pass

## Environment and Reliability Notes
- Evals execute against a running backend and consume real model/tool resources.
- Use a dedicated eval org/token and avoid production endpoints.
- Web3-related evals depend on available RPC/data providers.
- Keep evals deterministic enough for baseline comparisons.
