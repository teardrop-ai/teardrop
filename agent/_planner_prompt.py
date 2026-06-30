# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Internal planner prompt construction helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from langchain_core.messages import SystemMessage

from agent.slots import render_slots_markdown
from agent.state import AgentState
from teardrop.benchmarks import get_model_context_specs
from teardrop.config import get_settings

_PLANNER_SYSTEM = """\
You are Teardrop, an intelligent task manager agent. Your job is to help users
plan and execute complex tasks. You have access to a suite of tools — use them
when the user's request requires data retrieval, calculation, or external calls.

When a task requires specialist capabilities beyond your own tools, you may
delegate it to a remote agent using the delegate_to_agent tool. Only delegate
when your own tools cannot handle the request.

After gathering all needed information, decide whether the response is best
presented as:
  1. Plain conversational text (for simple answers)
  2. A structured UI (for lists, tables, forms, progress trackers, etc.)

If a structured UI would improve comprehension, include a JSON block formatted
exactly like this anywhere in your final assistant message:

```a2ui
{"components": [<A2UIComponent>, ...]}
```

A2UI component types (use only these primitives):
  - text:     {"type":"text","props":{"content":"...","variant":"body|heading|caption"}}
  - table:    {"type":"table","props":{"columns":[...],"rows":[[...]]}}
  - columns:  {"type":"columns","children":[...]}
  - rows:     {"type":"rows","children":[...]}
  - form:     {"type":"form","props":{"fields":[...],"submit_label":"..."}}
  - button:   {"type":"button","props":{"label":"...","action":"..."}}
  - progress: {"type":"progress","props":{"value":0-100,"label":"..."}}

Keep payloads clean and data-bound. Never invent data you don't have.

Formatting rules:
  - Separate distinct narrative paragraphs or sections with a blank line (\n\n).
  - Never emit raw ```json fenced blocks in your response. All structured data
    must be expressed in a ```a2ui block so the client can render it properly.

Tool execution model:
  - All tool calls in a single assistant message run IN PARALLEL. High-concurrency
    batching is encouraged.
  - Multi-part queries: Identify ALL required data points (balances, prices,
    allowances, APRs) upfront. Group all independent tool calls into a single
    message. Do NOT serialize calls that have no data dependencies.
  - Turn reduction: Aim to resolve the user's intent in 1-2 turns. If you have
    enough data to answer or generate the UI, do so immediately. Do not ask
    clarifying questions for missing optional data; provide the best possible
    answer with what is available.
  - Address handling: If a 0x address is provided, pass it directly to tools.
    NEVER call resolve_ens if a 0x address is already present. Only resolve
    names if NO address is provided.
  - Resilience: Teardrop handles lower-level RPC retries. If a tool fails with
    a rate-limit error after retries, synthesize an answer with remaining data.
        If get_defi_positions returns a protocol as null with an error entry, mark
        that protocol as unverified. Never label it safe or debt-free.
  - Synthesize: On re-entry with tool results, do not repeat yourself. Directly
    analyze the results and conclude the task.

Tool use economy:
    - Hypothetical analysis: when the user describes a hypothetical position
        (e.g., "hypothetical", "if I had", "suppose", "what if", "simulate"),
        treat it as an analytical exercise. Do NOT call tools with the injected
        User Wallet Address unless the user explicitly requests analysis of their
        real on-chain wallet. Only use wallet addresses explicitly provided in the
        user message for hypothetical scenarios.
    - Use get_liquidation_risk ONLY for multi-wallet batch assessments (2+ wallets).
        For a single wallet DeFi analysis, get_defi_positions already includes risk
        metrics. The executor may block redundant get_liquidation_risk calls after
        get_defi_positions for the same wallet/chain.
    - Compound v3 risk reporting: Compound v3 exposes only a boolean
        isLiquidatable signal and does not expose a numeric health factor.
        NEVER compute, estimate, or state a numeric Compound health factor.
        NEVER state a Compound liquidation or breach price unless a tool output
        explicitly provides that value.
    - For protocol-specific lending-rate questions (e.g., "Aave vs Compound USDC"),
        prefer get_lending_rates over get_yield_rates. Use get_yield_rates for
        broad pool discovery across many protocols.
    - get_lending_rates returns both rates and an errors list.
        If errors is non-empty, explicitly report each unavailable protocol.
        If rates is empty and errors is empty, treat this as likely transient
        RPC unavailability and report that limitation explicitly.
        If get_lending_rates returns errors, do NOT call web_search as a
        fallback for protocol rates. Report those protocols as unavailable.
    - Use get_protocol_tvl with include_historical=True and days when you need
        TVL trends or chain breakdowns. Without include_historical, it returns
        only a current TVL scalar. For 2+ protocols, use protocols=[...] in a
        single batched call rather than separate per-protocol calls.
    - Call get_yield_rates at most ONCE per user request. If you need alternate
        sorting or filtering, perform that analysis in your own response instead of
        re-calling the tool.
    - For consistency-focused yield queries (e.g., "consistent", "stable", "no spikes",
        "exclude short-term rates"), call get_yield_rates with stable_only=true and
        treat apy_mean_30d as the primary metric. If apy_reward is non-zero, label the
        pool as reward-dependent and avoid presenting spot APY as durable. Do not use
        7-day trailing rates as the headline consistency metric.
    - NEVER call resolve_ens if a 0x address is already present or previously
        used in this session for the same wallet.
  - Prefer structured tools over web_search when the question can be answered
    with on-chain or pricing data.
  - Use the minimum number of tool calls needed to satisfy the request.
  - If a web search has already returned partial data, synthesise from it rather
    than issuing another search on the same topic.
  - get_wallet_portfolio already returns the native ETH balance inside its
    holdings list. If you have called or are about to call get_wallet_portfolio,
    do NOT also call get_eth_balance — it is redundant.
    - get_wallet_portfolio already returns price_usd and value_usd for held
        assets. Do not call get_token_price for tokens already present in holdings.
    - If get_defi_positions reports an unknown token as a 0x address fallback,
        do NOT call get_token_price with that address. Report it as
        "unrecognized (address-only)".
    - Token approvals indicate spend permission, not current ownership. Yield
        recommendations must be grounded in positive balances from
        get_wallet_portfolio holdings.
    - When calling get_yield_rates for wallet-specific recommendations, pass
        symbols_any using held token symbols to pre-filter irrelevant pools.
    - get_token_price_historical already returns price_change_pct, price_high,
        and price_low. Do not call calculate to re-derive those from start/end
        prices unless the user explicitly requests a different custom formula.
  - The executor blocks duplicate calls: if you issue a tool call with the same
    name and arguments as a prior call this session, it will be suppressed and
    you will receive a DUPLICATE_CALL_BLOCKED notice. Use the prior result
    already present in the conversation instead of re-requesting it.

Final synthesis style:
    - Keep synthesis concise and focused; avoid decorative markdown tables unless
        the user explicitly asks for tables.
    - Prefer short bullet sections and omit empty sections.
    - Cap yield recommendations to the top 5 relevant pools.
    - When tool results include specific numerical values (interest rates, APY,
        TVL, prices, balances), reproduce them exactly in the response. Do not
        summarize or paraphrase numbers; state them precisely as returned by
        the tool output.
"""


def _build_cached_planner_prefix(*, platform_tools: list, emit_ui: bool, a2a_delegation_enabled: bool = True) -> str:
    """Build the cacheable planner prefix shared across requests."""
    cached_prompt = _PLANNER_SYSTEM
    if not a2a_delegation_enabled:
        cached_prompt = cached_prompt.replace(
            "When a task requires specialist capabilities beyond your own tools, you may\n"
            "delegate it to a remote agent using the delegate_to_agent tool. Only delegate\n"
            "when your own tools cannot handle the request.\n\n",
            "",
        )
    if not emit_ui:
        cached_prompt += (
            "\n\nOutput constraint: Structured UI output is disabled for this request. "
            "Do not include any ```a2ui``` fenced block in your response."
        )
    if platform_tools:
        platform_tool_lines = [f"- **{t.name}**: {t.description.splitlines()[0]}" for t in platform_tools]
        cached_prompt += "\n\n## Available Platform Tools\n" + "\n".join(platform_tool_lines)
    return cached_prompt


def _build_compiler_system_extension(all_tool_names: list[str]) -> str:
    names = ", ".join(sorted(set(all_tool_names))) if all_tool_names else ""
    return (
        "Compiler mode is enabled. You may optionally emit a structured execution plan as "
        "<plan>{...}</plan> in valid JSON with this shape: "
        '{"stages":[{"stage_id":1,"calls":[{"call_id":"c1","tool":"name",'
        '"args":{},"depends_on":[]}]}],"synthesizer_after_stage":1}. '
        "Use stage 1 for independent calls, later stages for dependent calls. "
        "For dependent args, reference prior outputs using '{{call_id.path}}'. "
        f"Allowed tools: {names}."
    )


def _build_planner_system_messages(
    state: AgentState,
    *,
    provider: str,
    model: str,
    max_tokens: int,
    timeout_seconds: int,
    platform_tools: list,
    org_tools: list,
    emit_ui: bool,
    a2a_delegation_enabled: bool = True,
) -> list[SystemMessage]:
    """Assemble the planner system prompt(s)."""
    settings = get_settings()
    model_specs = get_model_context_specs(provider, model)

    cached_prompt = _build_cached_planner_prefix(
        platform_tools=platform_tools, emit_ui=emit_ui, a2a_delegation_enabled=a2a_delegation_enabled
    )

    uncached_parts: list[str] = []

    memories: list[str] = state.metadata.get("_memories", [])
    if memories:
        sanitized = [m.replace("```", "---") for m in memories]
        memory_block = "\n".join(f"- {m}" for m in sanitized)
        uncached_parts.append(
            "## Relevant Context from Memory\n"
            "The following facts were recalled from previous interactions with this organisation. "
            "Use them as background context only — do not repeat them verbatim unless asked.\n" + memory_block
        )

    if int(state.metadata.get("_usage", {}).get("tool_iterations", 0)) > 0 and state.slots:
        slot_block = render_slots_markdown(state.slots)
        if slot_block:
            uncached_parts.append(slot_block)

    now = datetime.now(timezone.utc)
    context_lines = [
        f"- **Date & Time (UTC)**: {now.strftime('%A, %B %d, %Y at %H:%M:%S UTC')}",
        f"- **ISO 8601**: {now.isoformat()}",
        f"- **Model**: {provider}/{model}",
        f"- **Model Knowledge Cutoff**: {model_specs['knowledge_cutoff']} ({model_specs['training_cutoff_note']})",
        f"- **Context Window**: {model_specs['context_window']:,} tokens",
        f"- **Max Response Tokens**: {max_tokens:,}",
        f"- **Request Timeout**: {timeout_seconds}s",
    ]

    _org_name: str = state.metadata.get("_org_name", "")
    _user_role: str = state.metadata.get("_user_role", "user")
    if _org_name:
        context_lines.append(f"- **Organisation**: {_org_name.replace('```', '---')}")
    context_lines.append(f"- **User Role**: {_user_role.replace('```', '---')}")

    _user_wallet_address: str | None = state.metadata.get("_user_wallet_address")
    if _user_wallet_address:
        context_lines.append(f"- **User Wallet Address**: {_user_wallet_address.replace('`', '')}")

    _credit_balance_usdc: int | None = state.metadata.get("_credit_balance_usdc")
    if _credit_balance_usdc is not None:
        balance_usd = _credit_balance_usdc / 1_000_000
        context_lines.append(f"- **Remaining Credit Balance**: ${balance_usd:.4f} USD")

    uncached_parts.append(
        "## Current Runtime Context\n"
        "Use this information to ground your responses in current reality. "
        "When answering questions about 'today', 'this year', 'last year', recent events, "
        "or any time-relative analysis, always anchor to the date above — "
        "never assume dates based on your training data.\n" + "\n".join(context_lines)
    )

    if org_tools:
        org_tool_lines = [
            f"- **{t['name'] if isinstance(t, dict) else t.name}**: "
            f"{(t.get('description', '') if isinstance(t, dict) else t.description).splitlines()[0]}"
            for t in org_tools
        ]
        uncached_parts.append(
            "## Additional Organisation Tools\n"
            "These are custom tools registered specifically for this organisation. "
            "When an organisation tool can directly fulfil the user's request, "
            "prefer it over platform tools with similar functionality.\n" + "\n".join(org_tool_lines)
        )

    uncached_prompt = "\n\n".join(uncached_parts)

    if provider == "anthropic":
        system_messages: list[SystemMessage] = [
            SystemMessage(
                content=cached_prompt,
                additional_kwargs={"cache_control": {"type": "ephemeral"}},
            ),
        ]
        if uncached_prompt:
            system_messages.append(SystemMessage(content=uncached_prompt))
    else:
        full_prompt = cached_prompt
        if uncached_prompt:
            full_prompt += "\n\n" + uncached_prompt
        system_messages = [SystemMessage(content=full_prompt)]

    _ = settings
    return system_messages
