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

logger = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = ("429", "rate limit", "too many requests", "exceeded", "throttl")

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
  - All tool calls in a single assistant message run IN PARALLEL.
  - For multi-part user queries, plan the FULL set of independent tool calls
    and emit them in ONE message. Do not serialize unrelated tasks across
    multiple turns when they have no data dependency on each other.
  - Read inputs literally. If the user provides a 0x address anywhere in their
    message, NEVER call resolve_ens — even if an ENS name is also mentioned.
    Pass the 0x address directly to all tools. Only call resolve_ens when the
    user supplies ONLY an ENS name and no 0x address is present.
  - On tool error or rate-limit, do NOT retry the same call. Proceed with the
    partial data you have and note the gap in the final answer.
  - Never use web_search to identify token contracts, addresses, or transaction
    hashes. If a tool returns an address with no symbol, report it as
    "unrecognized token (0x…)" and move on. Identifying random contracts via
    web search is unreliable and burns the iteration budget.
  - On re-entry (when ToolMessages are already present in the conversation):
    do NOT restate the original plan. Directly state what the new data shows
    and what action you are taking next.

Tool use economy:
  - Prefer structured tools over web_search when the question can be answered
    with on-chain or pricing data.
  - Use the minimum number of tool calls needed to satisfy the request.
  - If a web search has already returned partial data, synthesise from it rather
    than issuing another search on the same topic.
  - get_wallet_portfolio already returns the native ETH balance inside its
    holdings list. If you have called or are about to call get_wallet_portfolio,
    do NOT also call get_eth_balance — it is redundant.
    - Use get_liquidation_risk ONLY for multi-wallet batch assessments (2+ wallets).
        For a single wallet DeFi analysis, get_defi_positions already includes risk
        metrics. Do not call both tools for the same wallet unless explicitly asked.
  - The executor blocks duplicate calls: if you issue a tool call with the same
    name and arguments as a prior call this session, it will be suppressed and
    you will receive a DUPLICATE_CALL_BLOCKED notice. Use the prior result
    already present in the conversation instead of re-requesting it.
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


def _get_fallback_llm(*, failed_provider: str, failed_model: str):
    """Return a fallback LLM from default_model_pool, or None if unavailable."""
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

        return create_llm_from_config(
            {
                "provider": provider,
                "model": model,
                "api_key": api_key,
                "max_tokens": settings.agent_max_tokens,
                "temperature": settings.agent_temperature,
                "timeout_seconds": settings.agent_llm_timeout_seconds,
            }
        )

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
        logger.error("planner_node: LLM call timed out after %dms (timeout=%ss, provider=%s, model=%s)", elapsed, timeout_seconds, provider, model)
        return {
            "messages": [AIMessage(content="The AI model timed out. Please try again.")],
            "task_status": TaskStatus.FAILED,
            "error": "LLM timeout",
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


def _accumulate_usage(state: AgentState, response: AIMessage) -> dict[str, int]:
    """Add this turn's token counts to the running usage in metadata."""
    usage = dict(state.metadata.get("_usage", {}))
    extracted = extract_usage(response)
    usage["tokens_in"] = usage.get("tokens_in", 0) + extracted["tokens_in"]
    usage["tokens_out"] = usage.get("tokens_out", 0) + extracted["tokens_out"]
    return usage


async def planner_node(state: AgentState) -> dict[str, Any]:
    """Reasoning / planning node.  Calls the LLM with bound tools."""
    logger.debug("planner_node: entry, %d messages", len(state.messages))
    settings = get_settings()
    tools = _get_cached_tools()
    org_tools = state.metadata.get("_org_tools", [])
    all_tools = tools + org_tools
    llm_config = state.metadata.get("_llm_config")
    llm = get_llm_for_request(llm_config).bind_tools(all_tools)  # type: ignore[arg-type]

    _cfg = llm_config or {}
    _provider = _cfg.get("provider", settings.agent_provider)
    _model = _cfg.get("model", settings.agent_model)
    _max_tokens = _cfg.get("max_tokens", settings.agent_max_tokens)
    _timeout = _cfg.get("timeout_seconds", settings.agent_llm_timeout_seconds)

    system_messages = _build_planner_system_messages(
        state,
        provider=_provider,
        model=_model,
        max_tokens=_max_tokens,
        timeout_seconds=_timeout,
        platform_tools=tools,
        org_tools=org_tools,
    )
    messages = [*system_messages, *state.messages]

    result = await _invoke_planner_llm(
        llm,
        messages,
        settings.agent_llm_timeout_seconds,
        provider=_provider,
        model=_model,
    )
    if isinstance(result, dict) and result.get("error_type") == "rate_limit" and not llm_config:
        fallback_llm = _get_fallback_llm(failed_provider=_provider, failed_model=_model)
        if fallback_llm is not None:
            logger.warning("planner_node: retrying with fallback LLM after rate limit")
            fallback_bound = fallback_llm.bind_tools(all_tools)  # type: ignore[arg-type]
            result = await _invoke_planner_llm(
                fallback_bound,
                messages,
                settings.agent_llm_timeout_seconds,
            )
    if isinstance(result, dict):
        return result
    response: AIMessage = result

    usage = _accumulate_usage(state, response)

    return {
        "messages": [response],
        "task_status": TaskStatus.EXECUTING if response.tool_calls else TaskStatus.GENERATING_UI,
        "metadata": {**state.metadata, "_usage": usage},
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
    if tool is None:
        content = f"Tool '{tool_name}' not found."
    else:
        try:
            # delegate_to_agent needs org context for billing — call the raw
            # implementation directly so we can pass the config dict.
            if tool_name == "delegate_to_agent" and metadata:
                # Enforce per-run delegation quota
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
                    }

                from billing import _get_pool as _get_billing_pool
                from tools.definitions.delegate_to_agent import delegate_to_agent

                config = {
                    "configurable": {
                        "org_id": metadata.get("org_id", ""),
                        "run_id": metadata.get("run_id", ""),
                        "db_pool": _get_billing_pool(),
                        "jwt_token": metadata.get("_jwt_token"),
                    }
                }
                result = await delegate_to_agent(config=config, **tool_args)
            else:
                result = await tool.ainvoke(tool_args)
            content = json.dumps(result) if not isinstance(result, str) else result
        except Exception as exc:
            logger.warning("tool %s failed: %s", tool_name, exc)
            content = f"Tool error: {exc}"

    elapsed = int((time.monotonic() - start_mono) * 1000)
    logger.info("tool_executor: %s completed in %dms (result_len=%d)", tool_name, elapsed, len(content))
    return {
        "message": ToolMessage(content=content, tool_call_id=call_id),
        "name": tool_name,
        "elapsed_ms": elapsed,
        "content": content,
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

    try:
        import time
        batch_start_mono = time.monotonic()
        results = await asyncio.wait_for(
            asyncio.gather(*[_execute_single_tool(call, tools_by_name, state.metadata) for call in dedup_calls]),
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
    tool_names_acc.extend(r["name"] for r in results)

    # Record newly executed signatures so future iterations can dedup them.
    completed_sigs.extend(
        _call_signature(c["name"], c["args"])
        for c in dedup_calls
        if c["name"] in platform_tool_names
    )
    usage["_completed_calls"] = completed_sigs

    usage["tool_calls"] = usage.get("tool_calls", 0) + len(dedup_calls)
    usage["tool_iterations"] = usage.get("tool_iterations", 0) + 1
    usage["tool_names"] = tool_names_acc

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

    # --- Try to extract inline ```a2ui``` block first ---
    if isinstance(last_msg, AIMessage) and last_msg.content:
        text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
        components = _extract_a2ui_from_text(text)
        if components:
            return {
                "ui_components": components,
                "task_status": TaskStatus.COMPLETED,
            }

    # --- If no inline block and we have a data-rich response, ask LLM to generate ---
    if isinstance(last_msg, AIMessage) and last_msg.content:
        text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
        if _contains_structured_data(text):
            settings = get_settings()
            prompt = f"{_UI_GENERATOR_SYSTEM}\n\nAssistant message:\n{text}"
            try:
                llm_config = state.metadata.get("_llm_config")
                result: AIMessage = await asyncio.wait_for(  # type: ignore[assignment]
                    get_llm_for_request(llm_config).ainvoke(prompt),
                    timeout=settings.agent_ui_generator_timeout_seconds,
                )
                raw = result.content if isinstance(result.content, str) else str(result.content)
                components = _parse_a2ui_json(raw)
                if components:
                    return {
                        "ui_components": components,
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
