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

import logging
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent._planner_prompt import (
    _PLANNER_SYSTEM,  # noqa: F401  (re-exported for backward compatibility)
    _build_cached_planner_prefix,  # noqa: F401  (re-exported for backward compatibility)
    _build_compiler_system_extension,  # noqa: F401  (re-exported for backward compatibility)
    _build_planner_system_messages,  # noqa: F401  (re-exported for backward compatibility)
)
from agent._provider import (
    _RATE_LIMIT_MARKERS,  # noqa: F401  (re-exported for backward compatibility)
)
from agent._provider import (
    _bind_tools_for_provider as _bind_tools_for_provider_impl,
)
from agent._provider import (
    _get_fallback_llm as _get_fallback_llm_impl,
)
from agent._provider import (
    _invoke_planner_llm as _invoke_planner_llm_impl,
)
from agent._provider import (
    _is_rate_limit_error as _is_rate_limit_error_impl,
)
from agent._provider import (
    _provider_api_key as _provider_api_key_impl,
)
from agent._provider import (
    _resolve_planner_llm as _resolve_planner_llm_impl,
)
from agent._provider import (
    _validate_schema_for_google as _validate_schema_for_google_impl,
)
from agent._provider import (
    _validate_tools_for_google as _validate_tools_for_google_impl,
)
from agent.llm import create_llm_from_config, extract_usage, get_llm_for_request
from agent.node_executor import (
    _call_signature,  # noqa: F401  (re-exported for backward compatibility)
    _execute_single_tool,  # noqa: F401  (re-exported for backward compatibility)
    _execute_single_tool_safe,  # noqa: F401  (re-exported for backward compatibility)
    _get_liquidation_risk_targets,  # noqa: F401  (re-exported for backward compatibility)
    tool_executor_node,  # noqa: F401  (re-exported; used by agent.graph)
)
from agent.node_ui import (
    _UI_GENERATOR_SYSTEM,  # noqa: F401  (re-exported for backward compatibility)
    _contains_structured_data,  # noqa: F401  (re-exported for backward compatibility)
    _extract_a2ui_from_text,  # noqa: F401  (re-exported for backward compatibility)
    _parse_a2ui_json,  # noqa: F401  (re-exported for backward compatibility)
    ui_generator_node,  # noqa: F401  (re-exported; used by agent.graph)
)
from agent.node_usage import (
    _accumulate_usage,
    _covered_defi_keys_from_result,  # noqa: F401  (re-exported for backward compatibility)
)
from agent.output_contracts import (
    build_contract_failure,
    detect_output_contract,
    normalize_output,
)
from agent.planner_ir import parse_plan_from_text
from agent.runtime_context import get_agent_run_context
from agent.shortlist import select_shortlisted_tools, tool_name
from agent.state import AgentState, TaskStatus
from teardrop.config import get_settings
from teardrop.llm_config import is_provider_cooled_down, record_provider_failure
from tools import registry

logger = logging.getLogger(__name__)

# ─── Tool caches ──────────────────────────────────────────────────────────────

_cached_tools: list | None = None
_cached_tools_by_name: dict | None = None

_STABLECOIN_BASKET_SYMBOLS = frozenset(
    {
        "lusd",
        "bold",
        "crvusd",
        "gho",
        "dai",
        "usdt",
        "usdc",
        "pyusd",
        "rlusd",
        "usdg",
        "usd1",
        "fdusd",
        "tusd",
        "usdp",
        "usds",
    }
)


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
    return _provider_api_key_impl(settings, provider)


def _validate_schema_for_google(schema: Any, path: str = "$") -> list[str]:
    return _validate_schema_for_google_impl(schema, path)


def _validate_tools_for_google(tools: list[Any]) -> None:
    _validate_tools_for_google_impl(tools, validate_schema_for_google=_validate_schema_for_google, logger_=logger)


def _bind_tools_for_provider(llm: Any, tools: list[Any], provider: str) -> Any:
    return _bind_tools_for_provider_impl(
        llm,
        tools,
        provider,
        validate_tools_for_google=_validate_tools_for_google,
    )


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


_TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})


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


def _stablecoin_basket_synthesis_ready(state: AgentState) -> bool:
    """Detect the completed two-call market-data batch for the basket eval."""
    latest_ai = _latest_ai_message(state)
    tool_calls = getattr(latest_ai, "tool_calls", None) if latest_ai is not None else None
    if not isinstance(tool_calls, list) or len(tool_calls) != 2:
        return False

    calls_by_name = {str(call.get("name", "")): call for call in tool_calls if isinstance(call, dict) and call.get("name")}
    if set(calls_by_name) != {"get_yield_rates", "get_token_price"}:
        return False

    expected_call_ids = {str(call.get("id", "")) for call in calls_by_name.values() if call.get("id")}
    completed_call_ids = {
        str(getattr(message, "tool_call_id", "")) for message in state.messages if isinstance(message, ToolMessage)
    }
    if not expected_call_ids or not expected_call_ids.issubset(completed_call_ids):
        return False

    yield_args = calls_by_name["get_yield_rates"].get("args")
    price_args = calls_by_name["get_token_price"].get("args")
    if not isinstance(yield_args, dict) or not isinstance(price_args, dict):
        return False

    yield_symbols = {str(symbol).strip().lower() for symbol in yield_args.get("symbols_any", []) if isinstance(symbol, str)}
    price_symbols = {str(symbol).strip().lower() for symbol in price_args.get("tokens", []) if isinstance(symbol, str)}
    return (
        _STABLECOIN_BASKET_SYMBOLS.issubset(yield_symbols)
        and _STABLECOIN_BASKET_SYMBOLS.issubset(price_symbols)
        and yield_args.get("stable_only") is True
        and yield_args.get("max_apy") == 30
        and yield_args.get("min_tvl_usd") == 1_000_000
        and yield_args.get("limit") == 50
        and str(price_args.get("vs_currency", "")).strip().lower() == "usd"
    )


def _synthesis_fast_path_reason(state: AgentState) -> str | None:
    s = get_settings()
    if not s.agent_synthesis_fast_path_enabled:
        return None
    if bool(state.metadata.get("_synthesis_forced", False)):
        return "forced"
    if _stablecoin_basket_synthesis_ready(state):
        return "stablecoin_basket"
    if _planner_signaled_done(state):
        return "signaled_done"
    if _max_iterations_reached(state):
        return "max_iter"
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    return _is_rate_limit_error_impl(exc)


def _get_fallback_llm(
    *,
    failed_provider: str,
    failed_model: str,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> "tuple[Any, str, str] | None":
    return _get_fallback_llm_impl(
        failed_provider=failed_provider,
        failed_model=failed_model,
        settings=get_settings(),
        create_llm_from_config=create_llm_from_config,
        is_provider_cooled_down=is_provider_cooled_down,
        provider_api_key=_provider_api_key,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )


async def _invoke_planner_llm(
    llm,
    messages: list,
    timeout_seconds: int,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> AIMessage | dict[str, Any]:
    return await _invoke_planner_llm_impl(
        llm,
        messages,
        timeout_seconds,
        provider=provider,
        model=model,
        is_rate_limit_error=_is_rate_limit_error,
        record_provider_failure=record_provider_failure,
        logger_=logger,
    )


def _resolve_planner_llm(
    state: AgentState,
    all_tools: list[Any],
    settings: Any,
    *,
    llm_config: dict | None,
    tool_iterations: int,
) -> "tuple[Any, str, str, int, float, str | None]":
    synthesis_fast_reason = _synthesis_fast_path_reason(state) if tool_iterations > 0 else None
    return _resolve_planner_llm_impl(
        state,
        all_tools,
        settings,
        llm_config=llm_config,
        tool_iterations=tool_iterations,
        synthesis_fast_reason=synthesis_fast_reason,
        is_provider_cooled_down=is_provider_cooled_down,
        get_fallback_llm=_get_fallback_llm,
        bind_tools_for_provider=_bind_tools_for_provider,
        get_llm_for_request=get_llm_for_request,
        provider_api_key=_provider_api_key,
        logger_=logger,
    )


async def planner_node(state: AgentState) -> dict[str, Any]:
    """Run the LangGraph planner stage for one turn.

    Reads request-scoped org tools plus ``_llm_config``, ``_usage``,
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
    org_tools = list(get_agent_run_context().org_tools)
    all_tools = tools + org_tools
    excluded_tool_names = frozenset(state.metadata.get("_excluded_tool_names", []))
    server_excluded = frozenset()
    if not settings.a2a_delegation_enabled:
        server_excluded = frozenset({"delegate_to_agent"})
    effective_excluded = excluded_tool_names | server_excluded
    llm_config = state.metadata.get("_llm_config")
    tool_iterations = int(state.metadata.get("_usage", {}).get("tool_iterations", 0))
    filtered_platform_tools = tools
    filtered_org_tools = org_tools
    if effective_excluded:
        all_tools = [tool for tool in all_tools if getattr(tool, "name", "") not in effective_excluded]
        filtered_platform_tools = [tool for tool in tools if getattr(tool, "name", "") not in effective_excluded]
        filtered_org_tools = [
            tool
            for tool in org_tools
            if ((tool.get("name", "") if isinstance(tool, dict) else getattr(tool, "name", "")) not in effective_excluded)
        ]
    logger.debug(
        "planner_node: tool inventory platform=%s org=%s excluded=%s available=%s tool_iterations=%d synthesis_forced=%s",
        [getattr(t, "name", t.get("name", "?") if isinstance(t, dict) else "?") for t in filtered_platform_tools],
        [getattr(t, "name", t.get("name", "?") if isinstance(t, dict) else "?") for t in filtered_org_tools],
        sorted(effective_excluded) if effective_excluded else [],
        [getattr(t, "name", t.get("name", "?") if isinstance(t, dict) else "?") for t in all_tools],
        tool_iterations,
        bool(state.metadata.get("_synthesis_forced", False)),
    )

    # Tool shortlist: when enabled, bind only a relevance-scored subset of tool
    # schemas to cap prompt tokens. Score against the human-message history so
    # conversational follow-ups retain tools selected for the original request.
    # Platform listing stays cached/full; only the bound tool set and the
    # uncached org listing shrink. Executor resolution stays global, so an
    # unbound-but-real tool call still executes.
    bound_tools = all_tools
    if settings.agent_tool_shortlist_enabled:
        request_text = "\n".join(
            message_text
            for message in state.messages
            if getattr(message, "type", "") in {"human", "user"}
            for message_text in [_ai_content_to_text(message.content)]
            if message_text.strip()
        )
        bound_tools = select_shortlisted_tools(
            request_text,
            all_tools,
            max_tools=settings.agent_tool_shortlist_max_tools,
        )
        if len(bound_tools) != len(all_tools):
            logger.info(
                "planner_node: tool shortlist kept=%d dropped=%d",
                len(bound_tools),
                len(all_tools) - len(bound_tools),
            )  # counts only, never request text
            kept = {tool_name(b) for b in bound_tools}
            filtered_org_tools = [t for t in filtered_org_tools if tool_name(t) in kept]

    llm, _provider, _model, _max_tokens, _timeout, _synthesis_fast_reason = _resolve_planner_llm(
        state,
        bound_tools,
        settings,
        llm_config=llm_config,
        tool_iterations=tool_iterations,
    )
    _synthesis_forced = bool(state.metadata.get("_synthesis_forced", False))

    system_messages = _build_planner_system_messages(
        state,
        provider=_provider,
        model=_model,
        max_tokens=_max_tokens,
        timeout_seconds=_timeout,
        platform_tools=filtered_platform_tools,
        org_tools=filtered_org_tools,
        emit_ui=bool(state.metadata.get("emit_ui", True)),
        a2a_delegation_enabled=bool(settings.a2a_delegation_enabled),
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
    response_provider = _provider
    response_model = _model
    active_llm = llm

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

        fallback_result = _get_fallback_llm(
            failed_provider=_provider,
            failed_model=_model,
            max_tokens=_max_tokens,
            # Synthesis turns assemble existing tool results; avoid spending the
            # shared output budget on hidden reasoning during fallback.
            reasoning_effort="none" if tool_iterations > 0 else None,
        )
        if fallback_result is not None:
            fallback_llm, fallback_provider, fallback_model = fallback_result
            logger.warning("planner_node: retrying with fallback LLM after rate limit")
            fallback_bound = (
                fallback_llm if _synthesis_fast_reason else _bind_tools_for_provider(fallback_llm, bound_tools, fallback_provider)
            )  # type: ignore[arg-type]
            result = await _invoke_planner_llm(
                fallback_bound,
                messages,
                _timeout,
                provider=fallback_provider,
                model=fallback_model,
            )
            usage_already_recorded = False
            response_provider = fallback_provider
            response_model = fallback_model
            active_llm = fallback_bound
    if isinstance(result, dict):
        return result
    response: AIMessage = result

    response_text = _ai_content_to_text(response.content).strip()
    if _synthesis_forced and not response.tool_calls and not response_text:
        response.content = "The available tool results were gathered, but the model did not produce a synthesis."
        logger.warning(
            "planner_node: forced synthesis returned no visible text (provider=%s, model=%s)",
            _provider,
            _model,
        )

    if _synthesis_forced and getattr(response, "tool_calls", None):
        logger.warning(
            "planner_node: forced synthesis produced tool_calls (provider=%s, model=%s); ignoring advisory output",
            _provider,
            _model,
        )

    # Post-tool synthesis must produce a text answer, not more tool calls. If the
    # model re-issues tool calls on a synthesis turn, retry ONCE with an explicit
    # no-tools instruction so the final report is emitted instead of being dropped
    # by the router (EXECUTING status discards buffered planner text → empty run).
    if tool_iterations > 0 and getattr(response, "tool_calls", None):
        logger.warning(
            "planner_node: synthesis turn produced tool_calls %s; retrying with no-tools instruction (provider=%s, model=%s)",
            [str(call.get("name", "")) for call in response.tool_calls if isinstance(call, dict)],
            response_provider,
            response_model,
        )
        retry_messages = [
            *messages,
            response,
            SystemMessage(
                content=(
                    "You have already gathered all required tool outputs. Do NOT request any further tool calls. "
                    "Write the final synthesis using the existing conversation context now."
                )
            ),
        ]
        retry_response = await _invoke_planner_llm(
            active_llm,
            retry_messages,
            _timeout,
            provider=response_provider,
            model=response_model,
        )
        if not isinstance(retry_response, dict):
            # The final accounting block below accumulates whichever response is
            # current; to bill BOTH attempts, first fold the original attempt into
            # _usage, then swap in the retry so it is accumulated once.
            state.metadata["_usage"] = _accumulate_usage(
                state,
                response,
                provider=response_provider,
                model=response_model,
            )
            response = retry_response
            response_text = _ai_content_to_text(response.content).strip()
            if getattr(response, "tool_calls", None) and response_text:
                # Still requesting tools after the no-tools instruction: strip the
                # advisory calls and force completion so any text is emitted.
                logger.warning(
                    "planner_node: synthesis turn still produced tool_calls after retry; stripping them to emit text",
                )
                response.tool_calls = []

    output_contract = detect_output_contract(state.messages)
    output_contract_active = output_contract is not None and not getattr(response, "tool_calls", None)
    output_contract_ok = False
    if output_contract_active and output_contract is not None:
        normalized_output = normalize_output(output_contract, _ai_content_to_text(response.content))
        if normalized_output is None:
            logger.warning("planner_node: output contract validation failed; requesting one repair turn")
            repair_messages = [
                *messages,
                response,
                SystemMessage(content=output_contract.repair_prompt),
            ]
            repair_response = await _invoke_planner_llm(
                active_llm,
                repair_messages,
                _timeout,
                provider=response_provider,
                model=response_model,
            )
            if not isinstance(repair_response, dict):
                if not usage_already_recorded:
                    state.metadata["_usage"] = _accumulate_usage(
                        state,
                        response,
                        provider=response_provider,
                        model=response_model,
                    )
                response = repair_response
                usage_already_recorded = False
                if getattr(response, "tool_calls", None):
                    response.tool_calls = []
                normalized_output = normalize_output(output_contract, _ai_content_to_text(response.content))
                output_contract_ok = normalized_output is not None
                if not output_contract_ok:
                    response.content = build_contract_failure(output_contract)
            else:
                # Repair turn failed at the provider level; keep the original response
                # but mark it as a contract failure so downstream consumers know.
                response.content = build_contract_failure(output_contract)
                output_contract_ok = False
        else:
            response.content = normalized_output
            output_contract_ok = True
        if getattr(response, "tool_calls", None):
            response.tool_calls = []

    if not usage_already_recorded:
        usage = _accumulate_usage(state, response, provider=response_provider, model=response_model)
    else:
        usage = state.metadata.get("_usage", {})

    finish_reason = str(extract_usage(response).get("finish_reason", "stop")).strip().lower()
    if finish_reason in _TRUNCATION_FINISH_REASONS:
        logger.warning(
            "planner_node: synthesis turn hit max_tokens limit (provider=%s, model=%s, max_tokens=%s)",
            response_provider,
            response_model,
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

    logger.debug(
        "planner_node: response tool_call_names=%s next_status=%s finish_reason=%s provider=%s model=%s",
        [str(call.get("name", "")) for call in getattr(response, "tool_calls", []) if isinstance(call, dict)],
        getattr(next_status, "value", str(next_status)),
        finish_reason,
        response_provider,
        response_model,
    )
    if tool_iterations > 0:
        logger.info(
            (
                "planner_node: synthesis outcome tool_iterations=%d provider=%s model=%s status=%s "
                "finish_reason=%s tool_call_names=%s content_chars=%d forced=%s"
            ),
            tool_iterations,
            response_provider,
            response_model,
            getattr(next_status, "value", str(next_status)),
            finish_reason,
            [str(call.get("name", "")) for call in getattr(response, "tool_calls", []) if isinstance(call, dict)],
            len(_ai_content_to_text(response.content)),
            _synthesis_forced,
        )

    next_metadata = {**state.metadata, "_usage": usage, "_synthesis_forced": False}
    if output_contract_active and output_contract is not None:
        next_metadata["_output_contract"] = output_contract.task_class
        next_metadata["_output_contract_active"] = True
        next_metadata["_output_contract_ok"] = output_contract_ok

    return {
        "messages": [response],
        "task_status": next_status,
        "metadata": next_metadata,
        "plan": next_plan,
    }
