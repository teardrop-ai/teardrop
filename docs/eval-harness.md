# Eval Harness

The eval harness runs task suites against the agent and reports quality, latency, and cost in one pass.

## Quick Start

```powershell
python -m evals.cli --suite smoke --base-url http://localhost:8000 --token <jwt>
```

You can pass a suite name under evals/tasks (for example smoke, defi, wallet, research) or an explicit path.

## Task Format

Task files are stored in evals/tasks/*.yaml.

Each task supports:

- id
- messages
- expected_tool_calls
- expected_text_contains
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
python -m evals.cli --suite smoke --baseline-report .\baseline.json --output .\candidate.json
```

The CLI prints a markdown summary with task-level results and delta metrics.
