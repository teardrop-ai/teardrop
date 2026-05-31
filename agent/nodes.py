# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Graph node implementations for the Teardrop LangGraph agent.

Node pipeline:
  planner_node  →  (tool_executor_node ↩)  →  ui_generator_node  →  END

─── planner_node ─────────────────────────────────────────────────────────────
Calls the LLM with all bound tools (platform + org + MCP + marketplace).
Reads: messages, slots, plan, _org_tools, _llm_config, _usage, _excluded_tool_names.
Writes: task_status, plan, _usage (token counts via _accumulate_usage, per-turn
attribution for multi-provider turns), _synthesis_forced.
Handles provider cooldown and fallback routing, compiler mode, and forced
synthesis when all tool calls were suppressed.

─── tool_executor_node ───────────────────────────────────────────────────────
Executes all pending tool calls from the latest AIMessage in parallel.
Writes: billable_tool_calls, billable_tool_names (used for marketplace earnings
and the USAGE_SUMMARY SSE event), failed_tool_calls, tool_iterations,
delegation_spend_usdc. Within-run deduplication: identical (tool_name, args)
calls are suppressed. Per-tool call-count guards (agent_tool_max_calls_per_run)
enforced here.

─── ui_generator_node ────────────────────────────────────────────────────────
Parses or generates A2UI components from the agent's final AIMessage content.
Emits SURFACE_UPDATE events (separate from TEXT_MESSAGE_CONTENT).

─── _accumulate_usage ────────────────────────────────────────────────────────
Adds per-turn token counts (tokens_in, tokens_out, cache_read_tokens,
cache_creation_tokens) to the running _usage dict for USAGE_SUMMARY reporting.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.llm import create_llm_from_config, extract_usage, get_llm_for_request
from agent.node_executor import (
    _call_signature,  # noqa: F401  (re-exported for backward compatibility)
    _execute_single_tool,  # noqa: F401  (re-exported for backward compatibility)
    _execute_single_tool_safe,  # noqa: F401  (re-exported for backward compatibility)
    _get_liquidation_risk_targets,  # noqa: F401  (re-exported for backward compatibility)
    tool_executor_node,  # noqa: F401  (re-exported; used by agent.graph)
)
from agent.node_usage import (
    _accumulate_usage,
    _covered_defi_keys_from_result,  # noqa: F401  (re-exported for backward compatibility)
)
from agent.planner_ir import parse_plan_from_text
from agent.slots import render_slots_markdown
from agent.state import A2UIComponent, AgentState, TaskStatus
from teardrop.benchmarks import get_model_context_specs
from teardrop.config import get_settings
from teardrop.llm_config import is_provider_cooled_down, record_provider_failure
from tools import registry

logger = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate-limited",
    "too many requests",
    "exceeded",
    "throttl",
    "quanta",
)

# ─── Tool caches ──────────────────────────────────────────────────────────────

_cached_tools: list | None = None
_cached_tools_by_name: dict | None = None


def _get_cached_tools() -> list:
    global _cached_tools
    if _cached_tools is None:
        _cached_tools = registry.to_langchain_tools()
    return _cached_tools


def _get_cached_tools_by_name() -> dict:
    global _cached_tools_by_name
    if _cached_tools_by_name is None:
        _cached_tools_by_name = registry.get_langchain_tools_by_name()
    return _cached_tools_by_name


def _provider_api_key(settings, provider: str) -> str:
    """Resolve provider API key from global settings for per-turn overrides."""
    p = provider.lower()
    if p == "anthropic":
        return settings.anthropic_api_key or ""
    if p == "openai":
        return settings.openai_api_key or ""
    if p == "google":
        return settings.google_api_key or ""
    if p == "openrouter":
        return settings.openrouter_api_key or ""
    return ""


def _validate_schema_for_google(schema: Any, path: str = "$") -> list[str]:
    """Collect Gemini-incompatible array schema paths.

    Gemini function declarations require every array-typed node to include a
    non-empty ``items`` schema.
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    if schema.get("type") == "array":
        items = schema.get("items")
        if not isinstance(items, dict) or not items:
            errors.append(f"{path}: array is missing a non-empty 'items' schema")
        elif "type" not in items and any(k in items for k in ("anyOf", "oneOf", "allOf")):
            errors.append(
                f"{path}: array 'items' must declare a concrete 'type' for Gemini compatibility "
                "(combinators like anyOf/oneOf/allOf are not supported)"
            )

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop_name, prop_schema in properties.items():
            errors.extend(_validate_schema_for_google(prop_schema, f"{path}.{prop_name}"))

    items = schema.get("items")
    if isinstance(items, dict):
        errors.extend(_validate_schema_for_google(items, f"{path}.items"))

    return errors


def _validate_tools_for_google(tools: list[Any]) -> None:
    """Fail fast if any bound tool has a Gemini-incompatible args schema."""
    violations: list[str] = []

    for idx, tool in enumerate(tools):
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is None:
            continue

        try:
            if hasattr(args_schema, "model_json_schema"):
                schema = args_schema.model_json_schema()
            elif hasattr(args_schema, "schema"):
                schema = args_schema.schema()
            else:
                continue
        except Exception as exc:
            logger.warning("Skipping schema validation for tool %s: %s", getattr(tool, "name", idx), exc)
            continue

        errors = _validate_schema_for_google(schema)
        if errors:
            tool_name = getattr(tool, "name", f"index_{idx}")
            for err in errors:
                violations.append(f"{tool_name} (index {idx}) -> {err}")

    if violations:
        summary = "\n".join(violations)
        logger.error("Gemini tool schema preflight failed:\n%s", summary)
        raise ValueError(
            f"Google/Gemini tool schema validation failed. Array parameters must include a non-empty 'items' schema.\n{summary}"
        )


def _bind_tools_for_provider(llm: Any, tools: list[Any], provider: str) -> Any:
    """Bind tools with provider-specific preflight checks."""
    if provider == "google":
        _validate_tools_for_google(tools)
    return llm.bind_tools(tools)


def _ai_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    out.append(text)
            elif hasattr(block, "text"):
                text = getattr(block, "text")
                if isinstance(text, str):
                    out.append(text)
        return "".join(out)
    return str(content or "")


def _latest_ai_message(state: AgentState) -> AIMessage | None:
    for msg in reversed(state.messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _planner_signaled_done(state: AgentState) -> bool:
    """True when the latest AI message is synthesis text without tool calls."""
    msg = _latest_ai_message(state)
    if msg is None:
        return False
    if getattr(msg, "tool_calls", None):
        return False
    return bool(_ai_content_to_text(getattr(msg, "content", "")).strip())


def _max_iterations_reached(state: AgentState) -> bool:
    usage = state.metadata.get("_usage", {})
    iterations = int(usage.get("tool_iterations", 0))
    # Trigger the fast path one turn before router forces ui_generator.
    return iterations >= max(0, get_settings().agent_max_tool_iterations - 1)


def _all_tool_calls_resolved(state: AgentState) -> bool:
    """True when all tool calls from the latest AI message have ToolMessage results."""
    latest_ai = _latest_ai_message(state)
    if latest_ai is None:
        return False

    tool_calls = getattr(latest_ai, "tool_calls", None) or []
    if not tool_calls:
        return False

    pending_ids = {str(call_id) for call in tool_calls if isinstance(call, dict) and (call_id := call.get("id"))}
    if not pending_ids:
        return False

    resolved_ids: set[str] = set()
    for msg in reversed(state.messages):
        if msg is latest_ai:
            break
        if isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id:
                resolved_ids.add(str(tool_call_id))

    return pending_ids.issubset(resolved_ids)


def _synthesis_fast_path_reason(state: AgentState) -> str | None:
    s = get_settings()
    if not s.agent_synthesis_fast_path_enabled:
        return None
    if bool(state.metadata.get("_synthesis_forced", False)):
        return "forced"
    if _planner_signaled_done(state):
        return "signaled_done"
    if _all_tool_calls_resolved(state):
        return "all_resolved"
    if _max_iterations_reached(state):
        return "max_iter"
    return None


# ─── System prompts ───────────────────────────────────────────────────────────

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

_UI_GENERATOR_SYSTEM = """\
You are a UI layout assistant. The agent has finished its reasoning.
Review the final assistant message below and extract any ```a2ui``` block.
If no a2ui block is present but the response contains structured data (lists,
numbers, comparisons) that would benefit from a visual presentation, generate
one. Output ONLY valid JSON matching the schema:
{"components": [<A2UIComponent>, ...]}
No markdown, no prose — pure JSON.
"""


# ─── Nodes ────────────────────────────────────────────────────────────────────


def _build_cached_planner_prefix(*, platform_tools: list, emit_ui: bool) -> str:
    """Build the cacheable planner prefix shared across requests."""
    cached_prompt = _PLANNER_SYSTEM
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
) -> list[SystemMessage]:
    """Assemble the planner system prompt(s).

    Splits into a cacheable static prefix (base prompt + platform tools) and
    a per-request uncached suffix (memories, runtime context, org tools).
    For Anthropic, the prefix is emitted as a separate SystemMessage with
    ``cache_control`` so prompt caching applies. Other providers receive a
    single merged SystemMessage.
    """
    settings = get_settings()
    model_specs = get_model_context_specs(provider, model)

    # ── Cacheable prefix: base prompt + platform tools summary ────────────
    # This block is identical across all requests for the same org+model and is
    # eligible for Anthropic prompt caching (90% discount on cache reads).
    # It must NOT contain any org-specific data (memories, org name, balance)
    # to prevent cross-org cache pollution.
    cached_prompt = _build_cached_planner_prefix(platform_tools=platform_tools, emit_ui=emit_ui)

    # ── Uncached suffix: org memory + runtime context + org-specific tools ─
    uncached_parts: list[str] = []

    memories: list[str] = state.metadata.get("_memories", [])
    if memories:
        # Guard against prompt injection from stored memory content.
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
        org_tool_lines = [f"- **{t.name}**: {t.description.splitlines()[0]}" for t in org_tools]
        uncached_parts.append("## Additional Organisation Tools\n" + "\n".join(org_tool_lines))

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

    # Reference settings to maintain a stable lookup point for tests that patch
    # ``get_settings`` (no functional effect — kept for parity with original).
    _ = settings
    return system_messages


def _is_rate_limit_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _RATE_LIMIT_MARKERS)


def _get_fallback_llm(*, failed_provider: str, failed_model: str) -> "tuple[Any, str, str] | None":
    """Return ``(llm, provider, model)`` for the first usable fallback in the pool, or ``None``.

    Returning the provider and model explicitly avoids relying on internal
    LangChain attributes (e.g. ``lc_kwargs``) that differ across provider
    classes and are not part of the public API.
    """
    settings = get_settings()
    for entry in settings.default_model_pool:
        provider = entry.get("provider", "")
        model = entry.get("model", "")
        if not provider or not model:
            continue
        if provider == failed_provider and model == failed_model:
            continue
        if is_provider_cooled_down(provider, model):
            continue

        api_key = ""
        if provider == "anthropic":
            api_key = settings.anthropic_api_key
        elif provider == "openai":
            api_key = settings.openai_api_key
        elif provider == "google":
            api_key = settings.google_api_key
        elif provider == "openrouter":
            api_key = settings.openrouter_api_key

        if not api_key:
            continue

        llm = create_llm_from_config(
            {
                "provider": provider,
                "model": model,
                "api_key": api_key,
                "max_tokens": settings.agent_max_tokens,
                "temperature": settings.agent_temperature,
                "timeout_seconds": settings.agent_llm_timeout_seconds,
            }
        )
        return llm, provider, model

    return None


async def _invoke_planner_llm(
    llm,
    messages: list,
    timeout_seconds: int,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> AIMessage | dict[str, Any]:
    """Call the bound LLM with timeout + exception handling.

    Returns the raw ``AIMessage`` on success, or a node-result ``dict`` on
    failure (so callers can return it directly).
    """
    import time

    start_mono = time.monotonic()
    try:
        response = await asyncio.wait_for(  # type: ignore[return-value]
            llm.ainvoke(messages),
            timeout=timeout_seconds,
        )
        elapsed = int((time.monotonic() - start_mono) * 1000)
        logger.info("planner_node: LLM call completed in %dms (provider=%s, model=%s)", elapsed, provider, model)
        return response
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start_mono) * 1000)
        logger.error(
            "planner_node: LLM call timed out after %dms (timeout=%ss, provider=%s, model=%s)",
            elapsed,
            timeout_seconds,
            provider,
            model,
        )
        return {
            "messages": [AIMessage(content="The AI model timed out. Please try again.")],
            "task_status": TaskStatus.FAILED,
            "error": "LLM timeout",
            "error_type": "timeout",
        }
    except Exception as exc:
        elapsed = int((time.monotonic() - start_mono) * 1000)
        if provider and model and _is_rate_limit_error(exc):
            record_provider_failure(provider, model)
            logger.warning("planner_node: provider rate limited after %dms for %s/%s", elapsed, provider, model)
            return {
                "messages": [AIMessage(content=f"I encountered an error: {exc}")],
                "task_status": TaskStatus.FAILED,
                "error": str(exc),
                "error_type": "rate_limit",
            }
        logger.error("planner_node error after %dms: %s", elapsed, exc)
        return {
            "messages": [AIMessage(content=f"I encountered an error: {exc}")],
            "task_status": TaskStatus.FAILED,
            "error": str(exc),
        }


async def planner_node(state: AgentState) -> dict[str, Any]:
    """Run the LangGraph planner stage for one turn.

    Reads ``state.metadata._org_tools``, ``_llm_config``, ``_usage``,
    ``_excluded_tool_names``, ``_synthesis_forced`` and ``state.plan`` (compiler mode).
    Resolves and invokes the LLM (with provider cooldown fallback if not BYOK).
    Binds all tools for non-synthesis turns; skips bind_tools on forced synthesis
    to reduce prompt size.

    Updates ``state.metadata._usage`` via ``_accumulate_usage`` for token billing:
    per-turn ``tokens_in``, ``tokens_out``, ``cache_read_tokens``,
    ``cache_creation_tokens``, and per-turn attribution in ``_usage.turns``.

    Returns a state patch with ``messages``, ``task_status``, ``metadata``, and
    ``plan``. Sets ``task_status = EXECUTING`` when tool calls are present;
    ``GENERATING_UI`` when the response is final.
    """
    logger.debug("planner_node: entry, %d messages", len(state.messages))
    settings = get_settings()
    if len(state.messages) > settings.agent_thread_warning_message_count:
        logger.warning(
            "planner_node: large thread history detected message_count=%d threshold=%d thread_id=%s",
            len(state.messages),
            settings.agent_thread_warning_message_count,
            str(state.metadata.get("thread_id", "")),
        )
    tool_chars = sum(len(str(m.content)) for m in state.messages if isinstance(m, ToolMessage))
    if tool_chars > settings.agent_thread_warning_tool_chars:
        logger.warning(
            "planner_node: large tool payload detected tool_chars=%d threshold=%d thread_id=%s",
            tool_chars,
            settings.agent_thread_warning_tool_chars,
            str(state.metadata.get("thread_id", "")),
        )
    tools = _get_cached_tools()
    org_tools = state.metadata.get("_org_tools", [])
    all_tools = tools + org_tools
    excluded_tool_names = frozenset(state.metadata.get("_excluded_tool_names", []))
    if excluded_tool_names:
        all_tools = [tool for tool in all_tools if getattr(tool, "name", "") not in excluded_tool_names]
    llm_config = state.metadata.get("_llm_config")
    tool_iterations = int(state.metadata.get("_usage", {}).get("tool_iterations", 0))

    _cfg = llm_config or {}
    _provider = _cfg.get("provider", settings.agent_provider)
    _model = _cfg.get("model", settings.agent_model)

    # P0 FIX: Check if the primary provider is cooled down. If so, and we aren't
    # using a custom BYOK config (where we must respect the user's choice),
    # skip to fallback.
    if not llm_config and is_provider_cooled_down(_provider, _model):
        logger.warning("Primary provider %s:%s is in cooldown; attempting fallback", _provider, _model)
        fallback_result = _get_fallback_llm(failed_provider=_provider, failed_model=_model)
        if fallback_result is not None:
            fallback_llm, _provider, _model = fallback_result
            llm = _bind_tools_for_provider(fallback_llm, all_tools, _provider)
        else:
            llm = _bind_tools_for_provider(get_llm_for_request(llm_config), all_tools, _provider)
    else:
        llm = _bind_tools_for_provider(get_llm_for_request(llm_config), all_tools, _provider)  # type: ignore[arg-type]

    _max_tokens = _cfg.get("max_tokens", settings.agent_max_tokens)
    _timeout = _cfg.get("timeout_seconds", settings.agent_llm_timeout_seconds)
    _synthesis_forced = bool(state.metadata.get("_synthesis_forced", False))
    _synthesis_fast_reason = _synthesis_fast_path_reason(state) if tool_iterations > 0 else None
    if tool_iterations > 0 and not llm_config:
        _max_tokens = settings.agent_synthesis_max_tokens
        # Optional override: route synthesis turns through a dedicated faster
        # model (e.g., gpt-4o-mini) to slash final-turn latency. Only applied
        # when both override fields are set and the override provider has a
        # usable API key — otherwise fall back to the primary planner model.
        _synth_provider = (settings.agent_synthesis_provider or "").strip()
        _synth_model = (settings.agent_synthesis_model or "").strip()
        if _synth_provider and _synth_model:
            _override_key = _provider_api_key(settings, _synth_provider)
            if _override_key and not is_provider_cooled_down(_synth_provider, _synth_model):
                _provider, _model = _synth_provider, _synth_model
        llm_unbound = create_llm_from_config(
            {
                "provider": _provider,
                "model": _model,
                "api_key": _provider_api_key(settings, _provider),
                "max_tokens": _max_tokens,
                "temperature": settings.agent_temperature,
                "timeout_seconds": _timeout,
            }
        )
        # When all calls were suppressed last turn, the planner is being asked
        # to synthesize from existing context — binding tool schemas only
        # bloats the request (5-8K tokens of unused JSON-Schema) without
        # any benefit. Skip bind_tools to cut prompt size and latency.
        if _synthesis_fast_reason:
            logger.debug("planner_node: synthesis fast path active (reason=%s)", _synthesis_fast_reason)
            llm = llm_unbound
        else:
            llm = _bind_tools_for_provider(llm_unbound, all_tools, _provider)
    elif tool_iterations == 0 and not llm_config:
        # Optional override: route the initial planning turn (tool selection)
        # through a dedicated fast model. Only applies when override fields are
        # set and the provider has a usable API key.
        _planner_provider = (settings.agent_planner_provider or "").strip()
        _planner_model = (settings.agent_planner_model or "").strip()
        if _planner_provider and _planner_model:
            _planner_key = _provider_api_key(settings, _planner_provider)
            if _planner_key and not is_provider_cooled_down(_planner_provider, _planner_model):
                _provider, _model = _planner_provider, _planner_model
                llm = create_llm_from_config(
                    {
                        "provider": _provider,
                        "model": _model,
                        "api_key": _planner_key,
                        "max_tokens": _max_tokens,
                        "temperature": settings.agent_temperature,
                        "timeout_seconds": _timeout,
                    }
                )
                llm = _bind_tools_for_provider(llm, all_tools, _provider)

    system_messages = _build_planner_system_messages(
        state,
        provider=_provider,
        model=_model,
        max_tokens=_max_tokens,
        timeout_seconds=_timeout,
        platform_tools=tools,
        org_tools=org_tools,
        emit_ui=bool(state.metadata.get("emit_ui", True)),
    )
    if settings.agent_compiler_mode_enabled:
        all_tool_names = [getattr(t, "name", "") for t in all_tools]
        system_messages.append(SystemMessage(content=_build_compiler_system_extension(all_tool_names)))
    if tool_iterations > 0:
        completed_names = state.metadata.get("_usage", {}).get("tool_names", [])
        unique_names = list(dict.fromkeys(str(name) for name in completed_names))
        completed_text = ", ".join(unique_names) if unique_names else "none"
        # Build a compact list of already-issued (tool, key-args) pairs from
        # prior AIMessage tool_calls in this run. Surfacing the actual arg
        # values (chain_id, wallet_address) — not just tool names — sharply
        # reduces the planner's tendency to re-issue the same call with
        # equivalent arguments, which would otherwise be suppressed by the
        # tool_executor and waste a full LLM round-trip on synthesis.
        prior_call_lines: list[str] = []
        _seen_sigs: set[str] = set()
        _arg_keys = ("chain_id", "wallet_address", "wallet_addresses", "protocol", "tokens")
        for _msg in state.messages:
            _calls = getattr(_msg, "tool_calls", None)
            if not _calls:
                continue
            for _c in _calls:
                _name = _c.get("name") if isinstance(_c, dict) else None
                _args = _c.get("args") if isinstance(_c, dict) else None
                if not _name or not isinstance(_args, dict):
                    continue
                _sig = _call_signature(_name, _args)
                if _sig in _seen_sigs:
                    continue
                _seen_sigs.add(_sig)
                _summary_parts = [f"{k}={_args[k]!r}" for k in _arg_keys if k in _args]
                _summary = ", ".join(_summary_parts) if _summary_parts else ""
                prior_call_lines.append(f"- {_name}({_summary})" if _summary else f"- {_name}()")
        use_prior_calls_block = not bool(state.slots)
        prior_calls_block = ""
        if use_prior_calls_block and prior_call_lines:
            prior_calls_block = "\n\nAlready issued tool calls (do NOT repeat with equivalent args):\n" + "\n".join(
                prior_call_lines
            )
        system_messages.append(
            SystemMessage(
                content=(
                    "Re-entry summary: tool iterations already completed this run. "
                    f"Completed tool calls: {completed_text}. "
                    "Do not repeat prior calls unless strictly required by new information. "
                    "Synthesize from existing tool results when possible." + prior_calls_block
                )
            )
        )
    if _synthesis_forced:
        system_messages.append(
            SystemMessage(
                content=(
                    "All planned tool calls for this turn were suppressed because they duplicate existing "
                    "results or exceeded safety caps. Do NOT request any further tool calls. "
                    "Write the final synthesis using the existing conversation context now."
                )
            )
        )
    planner_messages = state.messages
    if tool_iterations >= 2:
        tool_msgs = [m for m in state.messages if isinstance(m, ToolMessage)]
        keep_tool_ids = {id(m) for m in tool_msgs[-8:]}
        planner_messages = [m for m in state.messages if not isinstance(m, ToolMessage) or id(m) in keep_tool_ids]
        logger.debug(
            "planner_node: pruned %d old ToolMessages",
            max(0, len(tool_msgs) - len(keep_tool_ids)),
        )

    messages = [*system_messages, *planner_messages]
    recent_tool_messages = [m for m in planner_messages if isinstance(m, ToolMessage)][-8:]
    recent_tool_chars = sum(len(str(m.content)) for m in recent_tool_messages)
    logger.info(
        (
            "planner_node: invoking planner (tool_iterations=%d, "
            "recent_tool_messages=%d, recent_tool_chars=%d, provider=%s, "
            "model=%s, max_tokens=%s, timeout=%ss)"
        ),
        tool_iterations,
        len(recent_tool_messages),
        recent_tool_chars,
        _provider,
        _model,
        _max_tokens,
        _timeout,
    )

    result = await _invoke_planner_llm(
        llm,
        messages,
        _timeout,
        provider=_provider,
        model=_model,
    )

    # Track if we already recorded usage for 'result' to avoid double-counting if we retry
    usage_already_recorded = False

    if isinstance(result, dict) and result.get("error_type") == "rate_limit" and not llm_config:
        # Record usage for the failed turn BEFORE retrying, so we don't lose the tokens
        # if the failure was partial or provided usage metadata.
        if "messages" in result and isinstance(result["messages"][0], AIMessage):
            state.metadata["_usage"] = _accumulate_usage(
                state,
                result["messages"][0],
                provider=_provider,
                model=_model,
            )
            usage_already_recorded = True

        fallback_result = _get_fallback_llm(failed_provider=_provider, failed_model=_model)
        if fallback_result is not None:
            fallback_llm, fallback_provider, fallback_model = fallback_result
            logger.warning("planner_node: retrying with fallback LLM after rate limit")
            fallback_bound = (
                fallback_llm if _synthesis_fast_reason else _bind_tools_for_provider(fallback_llm, all_tools, fallback_provider)
            )  # type: ignore[arg-type]
            result = await _invoke_planner_llm(
                fallback_bound,
                messages,
                _timeout,
                provider=fallback_provider,
                model=fallback_model,
            )
    if isinstance(result, dict):
        return result
    response: AIMessage = result

    if _synthesis_forced and getattr(response, "tool_calls", None):
        logger.warning(
            "planner_node: forced synthesis produced tool_calls (provider=%s, model=%s); ignoring advisory output",
            _provider,
            _model,
        )

    if not usage_already_recorded:
        usage = _accumulate_usage(state, response, provider=_provider, model=_model)
    else:
        usage = state.metadata.get("_usage", {})

    finish_reason = str(extract_usage(response).get("finish_reason", "stop")).strip().lower()
    if finish_reason == "length":
        logger.warning(
            "planner_node: synthesis turn hit max_tokens limit (provider=%s, model=%s, max_tokens=%s)",
            _provider,
            _model,
            _max_tokens,
        )

    next_plan = state.plan
    if settings.agent_compiler_mode_enabled:
        try:
            parsed_plan = parse_plan_from_text(_ai_content_to_text(response.content))
            if parsed_plan is not None:
                known_tool_names = {getattr(t, "name", "") for t in all_tools}
                for stage in parsed_plan.stages:
                    for call in stage.calls:
                        if call.tool not in known_tool_names:
                            raise ValueError(f"Unknown tool in plan: {call.tool}")
                next_plan = parsed_plan
        except Exception as exc:
            logger.error("planner_node: invalid compiler plan ignored (%s)", exc)

    next_status = TaskStatus.GENERATING_UI
    if response.tool_calls or (next_plan is not None and not next_plan.is_done()):
        next_status = TaskStatus.EXECUTING

    return {
        "messages": [response],
        "task_status": next_status,
        "metadata": {**state.metadata, "_usage": usage, "_synthesis_forced": False},
        "plan": next_plan,
    }


async def ui_generator_node(state: AgentState) -> dict[str, Any]:
    """Parse or generate A2UI components from the agent's final message."""
    logger.debug("ui_generator_node: entry")
    last_msg = state.messages[-1] if state.messages else None
    if not isinstance(last_msg, AIMessage):
        for msg in reversed(state.messages):
            if isinstance(msg, AIMessage) and msg.content:
                last_msg = msg
                break
    emit_ui = bool(state.metadata.get("emit_ui", True))

    # --- Try to extract inline ```a2ui``` block first ---
    if isinstance(last_msg, AIMessage) and last_msg.content:
        text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
        components = _extract_a2ui_from_text(text)
        if components:
            return {
                "ui_components": [c.model_dump() for c in components],
                "task_status": TaskStatus.COMPLETED,
            }

    # --- If no inline block and we have a data-rich response, ask LLM to generate ---
    if emit_ui and isinstance(last_msg, AIMessage) and last_msg.content:
        text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
        if _contains_structured_data(text):
            settings = get_settings()
            prompt = f"{_UI_GENERATOR_SYSTEM}\n\nAssistant message:\n{text}"
            try:
                llm_config = state.metadata.get("_llm_config")
                if llm_config:
                    ui_llm = get_llm_for_request(llm_config)
                else:
                    ui_provider = settings.agent_ui_generator_provider
                    ui_model = settings.agent_ui_generator_model
                    ui_llm = create_llm_from_config(
                        {
                            "provider": ui_provider,
                            "model": ui_model,
                            "api_key": _provider_api_key(settings, ui_provider),
                            "max_tokens": settings.agent_synthesis_max_tokens,
                            "temperature": settings.agent_temperature,
                            "timeout_seconds": settings.agent_ui_generator_timeout_seconds,
                        }
                    )
                result: AIMessage = await asyncio.wait_for(  # type: ignore[assignment]
                    ui_llm.ainvoke(prompt),
                    timeout=settings.agent_ui_generator_timeout_seconds,
                )
                raw = result.content if isinstance(result.content, str) else str(result.content)
                components = _parse_a2ui_json(raw)
                if components:
                    return {
                        "ui_components": [c.model_dump() for c in components],
                        "task_status": TaskStatus.COMPLETED,
                    }
            except asyncio.TimeoutError:
                logger.warning("ui_generator_node: LLM call timed out")
            except Exception as exc:
                logger.warning("ui_generator_node: LLM call failed: %s", exc)

    return {"task_status": TaskStatus.COMPLETED}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _extract_a2ui_from_text(text: str) -> list[A2UIComponent]:
    """Extract components from a ```a2ui ... ``` fenced block."""
    import re

    pattern = r"```a2ui\s*(\{.*?\})\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return []
    return _parse_a2ui_json(match.group(1))


def _parse_a2ui_json(raw: str) -> list[A2UIComponent]:
    """Parse raw JSON string into A2UIComponent list."""
    try:
        data = json.loads(raw.strip())
        raw_components = data.get("components", [])
        return [A2UIComponent(**c) for c in raw_components]
    except Exception as exc:
        logger.debug("_parse_a2ui_json failed: %s", exc)
        return []


def _contains_structured_data(text: str) -> bool:
    """Heuristic: does the text contain tables, lists, or numeric data?"""
    import re

    indicators = [
        r"\|.*\|",  # Markdown table
        r"^\s*[-*]\s+",  # Bullet list
        r"\d+\.\s+\w+",  # Numbered list
        r"\b\d+[.,]\d+\b",  # Decimal numbers
    ]
    for pattern in indicators:
        if re.search(pattern, text, re.MULTILINE):
            return True
    return False
