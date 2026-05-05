# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Graph node implementations for the Teardrop LangGraph agent.

Node pipeline:
  planner  →  (tool_executor ↩)  →  ui_generator  →  END
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.llm import create_llm_from_config, extract_usage, get_llm_for_request
from agent.state import A2UIComponent, AgentState, TaskStatus
from benchmarks import get_model_context_specs
from config import get_settings
from llm_config import is_provider_cooled_down, record_provider_failure
from tools import registry
from tools.executor import execute_tool

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
            "Google/Gemini tool schema validation failed. "
            "Array parameters must include a non-empty 'items' schema.\n"
            f"{summary}"
        )


def _bind_tools_for_provider(llm: Any, tools: list[Any], provider: str) -> Any:
    """Bind tools with provider-specific preflight checks."""
    if provider == "google":
        _validate_tools_for_google(tools)
    return llm.bind_tools(tools)


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
    - Use get_liquidation_risk ONLY for multi-wallet batch assessments (2+ wallets).
        For a single wallet DeFi analysis, get_defi_positions already includes risk
        metrics. The executor may block redundant get_liquidation_risk calls after
        get_defi_positions for the same wallet/chain.
    - For protocol-specific lending-rate questions (e.g., "Aave vs Compound USDC"),
        prefer get_lending_rates over get_yield_rates. Use get_yield_rates for
        broad pool discovery across many protocols.
    - get_lending_rates returns both rates and an errors list.
        If errors is non-empty, explicitly report each unavailable protocol.
        If rates is empty and errors is empty, treat this as likely transient
        RPC unavailability and report that limitation explicitly.
    - Call get_yield_rates at most ONCE per user request. If you need alternate
        sorting or filtering, perform that analysis in your own response instead of
        re-calling the tool.
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
    cached_prompt = _PLANNER_SYSTEM
    if not emit_ui:
        cached_prompt += (
            "\n\nOutput constraint: Structured UI output is disabled for this request. "
            "Do not include any ```a2ui``` fenced block in your response."
        )
    if platform_tools:
        platform_tool_lines = [f"- **{t.name}**: {t.description.splitlines()[0]}" for t in platform_tools]
        cached_prompt += "\n\n## Available Platform Tools\n" + "\n".join(platform_tool_lines)

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


def _accumulate_usage(state: AgentState, response: AIMessage, *, provider: str, model: str) -> dict[str, Any]:
    """Add this turn's token counts to running usage and keep per-turn attribution."""
    usage = dict(state.metadata.get("_usage", {}))
    extracted = extract_usage(response)
    delta_in = int(extracted.get("tokens_in", 0))
    delta_out = int(extracted.get("tokens_out", 0))
    usage["tokens_in"] = int(usage.get("tokens_in", 0)) + delta_in
    usage["tokens_out"] = int(usage.get("tokens_out", 0)) + delta_out

    turns = usage.get("turns")
    if not isinstance(turns, list):
        turns = []
    turns.append(
        {
            "provider": str(provider or ""),
            "model": str(model or ""),
            "tokens_in": delta_in,
            "tokens_out": delta_out,
        }
    )
    usage["turns"] = turns
    return usage


def _covered_defi_keys_from_result(content: str) -> set[str]:
    """Return wallet+chain keys that have DeFi risk coverage from get_defi_positions.

    Coverage is recorded only when both Aave and Compound risk fetches did not
    fail for that wallet/chain. This avoids blocking get_liquidation_risk when
    risk-relevant protocol data is partial or unavailable.
    """
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return set()

    wallet = payload.get("wallet_address")
    chain_id = payload.get("chain_id")
    if not wallet or chain_id is None:
        return set()

    errors = payload.get("errors") or []
    failed_protocols = {str(err.get("protocol", "")).lower() for err in errors if isinstance(err, dict) and err.get("protocol")}
    # get_defi_positions risk coverage requires both Aave and Compound fetches.
    if "aave_v3" in failed_protocols or "compound_v3" in failed_protocols:
        return set()

    return {f"{int(chain_id)}:{str(wallet).lower()}"}


def _get_liquidation_risk_targets(call_args: dict[str, Any]) -> set[str]:
    """Build wallet+chain keys targeted by a get_liquidation_risk call."""
    chain_id = call_args.get("chain_id")
    wallets = call_args.get("wallet_addresses") or []
    if chain_id is None or not isinstance(wallets, list):
        return set()
    return {f"{int(chain_id)}:{str(addr).lower()}" for addr in wallets if addr}


async def planner_node(state: AgentState) -> dict[str, Any]:
    """Reasoning / planning node.  Calls the LLM with bound tools."""
    logger.debug("planner_node: entry, %d messages", len(state.messages))
    settings = get_settings()
    tools = _get_cached_tools()
    org_tools = state.metadata.get("_org_tools", [])
    all_tools = tools + org_tools
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
        llm = llm_unbound if _synthesis_forced else _bind_tools_for_provider(llm_unbound, all_tools, _provider)
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
        prior_calls_block = (
            "\n\nAlready issued tool calls (do NOT repeat with equivalent args):\n" + "\n".join(prior_call_lines)
            if prior_call_lines
            else ""
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
    messages = [*system_messages, *state.messages]
    recent_tool_messages = [m for m in state.messages if isinstance(m, ToolMessage)][-8:]
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
                fallback_llm
                if _synthesis_forced
                else _bind_tools_for_provider(fallback_llm, all_tools, fallback_provider)
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

    if not usage_already_recorded:
        usage = _accumulate_usage(state, response, provider=_provider, model=_model)
    else:
        usage = state.metadata.get("_usage", {})

    return {
        "messages": [response],
        "task_status": TaskStatus.EXECUTING if response.tool_calls else TaskStatus.GENERATING_UI,
        "metadata": {**state.metadata, "_usage": usage, "_synthesis_forced": False},
    }


def _call_signature(tool_name: str, tool_args: dict) -> str:
    """Stable hash for a (tool_name, args) pair — used for within-run dedup.

    sort_keys ensures key-ordering differences never produce a false miss.
    The 16-char prefix is sufficient for dedup at this scale and avoids
    storing full SHA-256 digests in state metadata.
    """
    import hashlib

    args_hash = hashlib.sha256(json.dumps(tool_args, sort_keys=True).encode()).hexdigest()[:16]
    return f"{tool_name}:{args_hash}"


async def _execute_single_tool(
    call: dict[str, Any], tools_by_name: dict, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Execute one tool call; returns result dict with metadata and ToolMessage.

    Errors are caught per-tool so a single failure does not abort sibling calls.
    For ``delegate_to_agent``, injects org context from *metadata* so billing
    and allowlist enforcement can operate.
    """
    import time

    tool_name: str = call["name"]
    tool_args: dict[str, Any] = call["args"]
    call_id: str = call["id"]
    start_mono = time.monotonic()

    tool = tools_by_name.get(tool_name)

    async def _delegate_invoke(**kwargs: Any) -> Any:
        from billing import _get_pool as _get_billing_pool
        from tools.definitions.delegate_to_agent import delegate_to_agent

        config = {
            "configurable": {
                "org_id": metadata.get("org_id", "") if metadata else "",
                "run_id": metadata.get("run_id", "") if metadata else "",
                "db_pool": _get_billing_pool(),
                "jwt_token": metadata.get("_jwt_token") if metadata else None,
            }
        }
        return await delegate_to_agent(config=config, **kwargs)

    if tool_name == "delegate_to_agent" and metadata:
        # Enforce per-run delegation quota.
        from config import get_settings as _get_settings

        _s = _get_settings()
        usage = metadata.get("_usage", {})
        delegation_count = usage.get("delegation_count", 0)
        if delegation_count >= _s.a2a_delegation_max_per_run:
            content = json.dumps(
                {
                    "agent_name": "unknown",
                    "status": "failed",
                    "result": "",
                    "error": f"Delegation quota exceeded: max {_s.a2a_delegation_max_per_run} per run",
                    "cost_usdc": 0,
                }
            )
            return {
                "message": ToolMessage(content=content, tool_call_id=call_id),
                "name": tool_name,
                "elapsed_ms": 0,
                "content": content,
                "billable": False,
            }

        result = await execute_tool(
            tool_name=tool_name,
            tool_call_id=call_id,
            tool_args=tool_args,
            invoke=_delegate_invoke,
        )
    else:
        result = await execute_tool(
            tool_name=tool_name,
            tool_call_id=call_id,
            tool_args=tool_args,
            tool=tool,
        )

    elapsed = int((time.monotonic() - start_mono) * 1000)
    content = result.content
    max_chars = get_settings().agent_max_tool_result_chars
    if max_chars > 0 and len(content) > max_chars:
        content = f"{content[:max_chars]}...[TRUNCATED: {len(result.content)} chars total]"
    logger.info("tool_executor: %s completed in %dms (result_len=%d)", tool_name, elapsed, len(content))
    return {
        "message": ToolMessage(content=content, tool_call_id=call_id),
        "name": tool_name,
        "elapsed_ms": elapsed,
        "content": content,
        "billable": result.billable,
        "error_class": result.error_class,
    }

async def _execute_single_tool_safe(
    call: dict,
    tools_by_name: dict,
    metadata: dict | None,
    per_tool_timeout_seconds: float | None,
) -> dict[str, Any]:
    """Run a single tool with a per-tool timeout and convert failures to ToolMessages."""
    import time

    start_mono = time.monotonic()
    tool_name = call.get("name", "unknown_tool")
    call_id = call.get("id", "unknown_call")
    try:
        if per_tool_timeout_seconds is None:
            return await _execute_single_tool(call, tools_by_name, metadata)
        return await asyncio.wait_for(
            _execute_single_tool(call, tools_by_name, metadata),
            timeout=per_tool_timeout_seconds,
        )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start_mono) * 1000)
        content = (
            f"[TOOL_TIMEOUT] '{tool_name}' exceeded the {per_tool_timeout_seconds}s per-tool timeout. "
            "Continue synthesis with available data."
        )
        logger.warning("tool_executor: %s per-tool timeout after %ss", tool_name, per_tool_timeout_seconds)
        return {
            "message": ToolMessage(content=content, tool_call_id=call_id),
            "name": tool_name,
            "elapsed_ms": elapsed,
            "content": content,
            "billable": False,
            "error_class": "timeout",
        }
    except Exception as exc:
        elapsed = int((time.monotonic() - start_mono) * 1000)
        content = (
            f"[TOOL_ERROR] '{tool_name}' failed unexpectedly ({type(exc).__name__}). "
            "Continue synthesis with available data."
        )
        logger.exception("tool_executor: %s unexpected failure", tool_name)
        return {
            "message": ToolMessage(content=content, tool_call_id=call_id),
            "name": tool_name,
            "elapsed_ms": elapsed,
            "content": content,
            "billable": False,
            "error_class": "unexpected_error",
        }


async def tool_executor_node(state: AgentState) -> dict[str, Any]:
    """Execute all pending tool calls in the latest AI message, in parallel."""
    logger.debug("tool_executor_node: entry")
    last_msg = state.messages[-1]
    if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
        return {"task_status": TaskStatus.GENERATING_UI}

    platform_tools_by_name = _get_cached_tools_by_name()
    tools_by_name = {
        **platform_tools_by_name,
        **state.metadata.get("_org_tools_by_name", {}),
    }
    platform_tool_names = set(platform_tools_by_name.keys())

    # ── Accumulate tool usage ─────────────────────────────────────────────
    usage = dict(state.metadata.get("_usage", {}))
    tool_names_acc: list[str] = list(usage.get("tool_names", []))
    failed_tool_names_acc: list[str] = list(usage.get("failed_tool_names", []))
    tool_call_counts: dict[str, int] = dict(usage.get("_tool_call_counts", {}))
    defi_positions_covered: set[str] = set(usage.get("_defi_positions_covered", []))
    tool_max_calls = dict(get_settings().agent_tool_max_calls_per_run)

    # ── Within-run deduplication ──────────────────────────────────────────
    # Calls with identical (tool_name, args) are suppressed so a re-entering
    # planner cannot re-fetch data that is already in the conversation.
    completed_sigs: list[str] = list(usage.get("_completed_calls", []))
    dedup_calls: list[dict] = []
    skipped_messages: list[ToolMessage] = []
    seen_this_batch: set[str] = set()
    for call in last_msg.tool_calls:
        if call["name"] not in platform_tool_names:
            dedup_calls.append(call)
            continue

        max_calls = tool_max_calls.get(call["name"])
        tool_meta = getattr(tools_by_name.get(call["name"]), "metadata", None)
        if isinstance(tool_meta, dict):
            meta_cap = tool_meta.get("max_calls_per_run")
            if isinstance(meta_cap, int) and meta_cap > 0:
                max_calls = meta_cap
        current_calls = int(tool_call_counts.get(call["name"], 0))
        # Account for same-tool calls already accepted in this batch.
        accepted_in_batch = sum(1 for c in dedup_calls if c["name"] == call["name"])
        if max_calls is not None and (current_calls + accepted_in_batch) >= int(max_calls):
            logger.debug("cap: suppressing capped call '%s'", call["name"])
            skipped_messages.append(
                ToolMessage(
                    content=(
                        f"[TOOL_CALL_CAP_EXCEEDED] '{call['name']}' exceeded the per-run cap "
                        f"({max_calls}). Reuse prior results already in the conversation."
                    ),
                    tool_call_id=call["id"],
                )
            )
            continue

        if call["name"] == "get_liquidation_risk":
            targets = _get_liquidation_risk_targets(call.get("args", {}))
            if targets and targets.issubset(defi_positions_covered):
                logger.debug("semantic: suppressing redundant call '%s'", call["name"])
                skipped_messages.append(
                    ToolMessage(
                        content=(
                            "[SEMANTIC_REDUNDANCY_BLOCKED] get_defi_positions already returned "
                            "risk-relevant data for the requested wallet(s)/chain. "
                            "Use those results instead of re-calling get_liquidation_risk."
                        ),
                        tool_call_id=call["id"],
                    )
                )
                continue

        if call["name"] == "get_token_price":
            tokens = (call.get("args") or {}).get("tokens") or []
            if isinstance(tokens, list) and tokens:
                normalized = [t.strip().lower() for t in tokens if isinstance(t, str) and t.strip()]
                if normalized and all(t.startswith("0x") for t in normalized):
                    logger.debug("semantic: suppressing unsupported get_token_price address-only call")
                    skipped_messages.append(
                        ToolMessage(
                            content=(
                                "[GET_TOKEN_PRICE_BLOCKED] All requested tokens are bare 0x addresses. "
                                "CoinGecko cannot resolve address-only identifiers. "
                                "Report these as unrecognized (address-only) and continue synthesis."
                            ),
                            tool_call_id=call["id"],
                        )
                    )
                    continue

        sig = _call_signature(call["name"], call["args"])
        if sig in completed_sigs or sig in seen_this_batch:
            logger.debug("dedup: suppressing duplicate call '%s'", call["name"])
            skipped_messages.append(
                ToolMessage(
                    content=(
                        f"[DUPLICATE_CALL_BLOCKED] '{call['name']}' was already called "
                        "with identical arguments this session. "
                        "Use the prior result already present in the conversation."
                    ),
                    tool_call_id=call["id"],
                )
            )
        else:
            seen_this_batch.add(sig)
            dedup_calls.append(call)

    if not dedup_calls:
        usage["tool_iterations"] = usage.get("tool_iterations", 0) + 1
        logger.info("tool_executor: all calls suppressed, routing to PLANNING for forced synthesis")
        return {
            "messages": skipped_messages,
            "task_status": TaskStatus.PLANNING,
            "metadata": {**state.metadata, "_usage": usage, "_synthesis_forced": True},
        }

    try:
        import time

        per_tool_timeout_seconds = float(get_settings().agent_single_tool_timeout_seconds)
        if per_tool_timeout_seconds <= 0:
            per_tool_timeout_seconds = None

        batch_start_mono = time.monotonic()
        results = await asyncio.wait_for(
            asyncio.gather(
                *[
                    _execute_single_tool_safe(call, tools_by_name, state.metadata, per_tool_timeout_seconds)
                    for call in dedup_calls
                ]
            ),
            timeout=get_settings().agent_tool_executor_timeout_seconds,
        )
        batch_elapsed = int((time.monotonic() - batch_start_mono) * 1000)
        durations = [r["elapsed_ms"] for r in results]
        slowest = max(durations) if durations else 0
        logger.info(
            "tool_executor: batch completed in %dms (num_tools=%d, slowest_tool=%dms)",
            batch_elapsed,
            len(results),
            slowest,
        )
    except asyncio.TimeoutError:
        logger.error("tool_executor_node timeout after %ss", get_settings().agent_tool_executor_timeout_seconds)
        return {
            "messages": [AIMessage(content="Tool execution timed out. Some tools took too long to respond.")],
            "task_status": TaskStatus.FAILED,
            "metadata": {**state.metadata, "_usage": usage},
        }

    tool_messages = skipped_messages + [r["message"] for r in results]

    billable_results = [r for r in results if bool(r.get("billable", True))]
    failed_results = [r for r in results if not bool(r.get("billable", True))]
    tool_names_acc.extend(r["name"] for r in billable_results)
    failed_tool_names_acc.extend(r["name"] for r in failed_results)

    # Record newly executed signatures so future iterations can dedup them.
    completed_sigs.extend(_call_signature(c["name"], c["args"]) for c in dedup_calls if c["name"] in platform_tool_names)
    usage["_completed_calls"] = completed_sigs

    for result in results:
        name = result["name"]
        tool_call_counts[name] = int(tool_call_counts.get(name, 0)) + 1
        if name == "get_defi_positions":
            defi_positions_covered.update(_covered_defi_keys_from_result(result.get("content", "")))

    usage["_tool_call_counts"] = tool_call_counts
    usage["_defi_positions_covered"] = sorted(defi_positions_covered)

    usage["tool_calls"] = usage.get("tool_calls", 0) + len(dedup_calls)
    usage["billable_tool_calls"] = usage.get("billable_tool_calls", 0) + len(billable_results)
    usage["failed_tool_calls"] = usage.get("failed_tool_calls", 0) + len(failed_results)
    usage["tool_iterations"] = usage.get("tool_iterations", 0) + 1
    usage["tool_names"] = tool_names_acc
    usage["billable_tool_names"] = tool_names_acc
    usage["failed_tool_names"] = failed_tool_names_acc

    # ── Accumulate delegation spend from delegate_to_agent results ────────
    delegation_spend = usage.get("delegation_spend_usdc", 0)
    delegation_count = usage.get("delegation_count", 0)
    for res in results:
        msg = res["message"]
        name = res["name"]
        if name == "delegate_to_agent":
            delegation_count += 1
            try:
                result_data = json.loads(msg.content)
                delegation_spend += int(result_data.get("cost_usdc", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
    usage["delegation_spend_usdc"] = delegation_spend
    usage["delegation_count"] = delegation_count

    return {
        "messages": tool_messages,
        "task_status": TaskStatus.PLANNING,
        "metadata": {**state.metadata, "_usage": usage},
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
