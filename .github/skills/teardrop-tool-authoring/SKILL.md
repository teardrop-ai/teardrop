---
name: teardrop-tool-authoring
argument-hint: "Describe the tool you want to add or the tool definition you want to review."
description: "Use when adding, reviewing, or editing a Teardrop tool in tools/definitions/. Defines the mandatory agent-consumer contract (use_when, limitations, alternatives), the fail-closed enforcement test, and the scaffold workflow."
disable-model-invocation: false
metadata: teardrop, tools, tool definition, ToolDefinition, registry, use_when, limitations, alternatives, agent commerce, mcp, a2a, agent card, scaffold, tool authoring, platform tools
user-invocable: true
---

# Teardrop Tool Authoring

Contract for every tool in `tools/definitions/`. Enforcement is fail-closed:
`tests/unit/test_tool_definition_standard.py` fails CI when any registered tool
violates it. Scaffold new tools with `scripts/scaffold_tool.py` so the fields
are required from the start.

## The contract

Every `ToolDefinition` must set, written for an **agent consumer** (never a
human operator):

| Field | Requirement |
|---|---|
| `description` | What decision the result supports. Self-sufficient (the LangChain planner sees only this field), ≤ 700 chars. |
| `use_when` | When to select this tool vs. skip it: batching/per-chain guidance, redundancy warnings, when to prefer a named alternative. 40–500 chars. |
| `limitations` | Staleness, caps, coverage, and what the output does **not** prove. 40–500 chars. |
| `alternatives` | ≥ 1 registered tool name; no self-reference; no dangling names. |
| `tags` | Non-empty. |

## Prohibited in agent-facing text

- Env-var setup strings (`Set TAVILY_API_KEY…`, `Requires ETHEREUM_RPC_URL`) —
  configuration belongs in `docs/configuration.md`.
- Human-workflow wording (`dashboard`, `settings page`, `click`).
- Restating what another tool already returns without a skip instruction.

## Reference implementations

- Composite pattern: `tools/definitions/assess_counterparty_risk.py`
- Primitive pattern: `tools/definitions/get_wallet_positions.py`
- Utility pattern: `tools/definitions/get_datetime.py`

## Where the fields surface

`tools/registry.py` exports them on the MCP server card (`to_mcp_server_card_tools`),
renders them into live MCP `tools/list` descriptions (`to_mcp_tool_defs`), and, for
`show_on_agent_card=True` tools, emits them on the A2A card (`to_a2a_skills`,
`to_a2a_tool_list`). LangChain tool binding (`to_langchain_tool`) passes
**`description` only** — so `description` must stand alone without
`use_when`/`limitations`.

## Workflow for a new tool

1. `python scripts/scaffold_tool.py --name my_tool --description "..." --use-when "..." --limitations "..." --alternative get_wallet_portfolio --tag web3` (dry-run by default; `--write` to create the file).
2. Implement the async callable and Pydantic input/output schemas in the stub.
3. Import and register the `TOOL` in `tools/definitions/__init__.py`.
4. Run `pytest tests/unit/test_tool_definition_standard.py -n 0` — it must pass with no `_EXEMPT` entry. Never add to `_EXEMPT`.
5. Bump `version` only for input/output schema or behavior changes. Guidance-text edits alone do not warrant a bump.
