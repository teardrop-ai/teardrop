# Eval Harness

The eval harness runs task suites against the agent and reports quality, latency, and cost in one pass.

## Quick Start

```powershell
python -m evals.cli --suite smoke --base-url http://localhost:8000 --token <jwt>
```

You can pass a suite name under evals/tasks (for example smoke, defi, wallet, research, marketplace, a2a) or an explicit path.

For rubric-based tasks, set `ANTHROPIC_API_KEY` so `scorer: "llm_judge"` can call the shared Anthropic client dependency already used by the agent stack.

## Policy Gates

Use a policy file to fail a run on pass-rate, cost, latency, or regression violations.

```powershell
python -m evals.cli --suite smoke --base-url http://localhost:8000 --token <jwt> --policy-file .\evals\policies\smoke.json
```

The CLI exits non-zero on policy violations by default. Use `--no-fail-on-regression` to warn without failing.

Example policy file:

```json
{
  "min_pass_rate": 1.0,
  "max_avg_cost_usdc": 600000,
  "max_latency_regression_pct": 50.0
}
```

## Task Format

Task files are stored in evals/tasks/*.yaml.

Each task supports:

- id
- messages
- expected_tool_calls
- expected_text_contains
- expected_text_not_contains
- max_duration_ms
- max_cost_usdc
- scorer
- rubric

Example:

```json
[
  {
    "id": "defi.balance_check.001",
    "messages": [{"role": "user", "content": "What is the USDC balance of vitalik.eth on Base?"}],
    "expected_tool_calls": ["resolve_ens", "get_erc20_balance"],
    "expected_text_contains": ["USDC", "vitalik"],
    "max_duration_ms": 8000,
    "max_cost_usdc": 500000,
    "scorer": "contains",
    "rubric": "Resolves ENS and returns a USDC balance."
  }
]
```

## Baseline Diff

Save a previous run as JSON and compare:

```powershell
python -m evals.cli --suite smoke --base-url http://localhost:8000 --token <jwt> --baseline-report .\baseline.json --output .\candidate.json
```

The CLI prints a markdown summary with task-level results and delta metrics.

## LLM Judge

Set `scorer` to `llm_judge` when substring matching is too weak and the task has a stable rubric.

```json
[
  {
    "id": "research.summary.002",
    "messages": [{"role": "user", "content": "Summarize the latest ETH ETF headlines."}],
    "expected_text_contains": ["ETH", "ETF"],
    "scorer": "llm_judge",
    "rubric": "Uses current information, stays concise, and mentions ETH ETF developments."
  }
]
```

If the Anthropic API key is missing or a test key is used, the harness falls back to deterministic contains-based scoring. For safety, tasks with no deterministic fallback expectations score `0.0` in that mode instead of silently passing.

### When to Use Each Scorer

| Scorer | Use Case | Tasks |
|--------|----------|-------|
| **contains** | Tool routing, fact retrieval, keyword verification | `smoke.*`, `defi.balance_check.001`, `defi.protocol_tvl.001`, `wallet.portfolio.001`, `marketplace.catalog.001`, `a2a.*` |
| **llm_judge** | Comparison accuracy, summary quality, nuanced constraints, semantic gates | `defi.lending_rates.001`, `defi.consistent_yield.001`, `research.summary.001`, `marketplace.platform_tools.001` |

**`llm_judge` tasks** require semantic correctness beyond keyword presence. For example:
- `defi.lending_rates.001` — must compare APY across two protocols with concrete figures
- `defi.consistent_yield.001` — must avoid short-term/spiky yield recommendations (keyword absence alone is insufficient)
- `research.summary.001` — quality and conciseness are unmeasurable by substring matching
- `marketplace.platform_tools.001` — must distinguish "platform tools do not require subscriptions" from incorrect claims (critical gate for API correctness)

**Contains fallback in CI**: All `llm_judge` tasks contain `expected_text_contains` keywords as a CI fallback. When running in CI under a test API key, the judge is skipped and the harness scores against keywords only. This catches gross failures but cannot verify semantic correctness — full fidelity requires a real `ANTHROPIC_API_KEY`.

## CI

The default CI test suite runs `pytest tests/unit/` and includes the eval harness unit tests (`test_eval_judge.py`, `test_eval_policy.py`, `test_eval_scorer.py`, `test_eval_harness_smoke.py`). These execute locally with a test API key, so all `llm_judge` tasks fall back to contains-based scoring. This is a documented tradeoff — CI gates catch structural failures (tool routing, keyword presence); semantic correctness gates are gated on real-key manual runs or staging deployments.

For staging evals with real credentials, run the CLI manually with a valid `ANTHROPIC_API_KEY` and an API token pointing to the staging backend.

Example:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python -m evals.cli --suite defi --base-url https://staging.teardrop.ai --token <jwt> --judge-model claude-haiku-4-5-20251001
```
