# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Tool execution stage for the Teardrop LangGraph agent.

Implements ``tool_executor_node`` and its helpers, factored out of
``agent.nodes`` for semantic clarity and focused testing. All public symbols are
re-exported from ``agent.nodes`` for backward compatibility, so existing imports
of ``agent.nodes.tool_executor_node`` and ``agent.nodes._call_signature``
continue to work.

Responsibilities:
  * ``tool_executor_node`` — runs all pending tool calls from the latest
    AIMessage (or the active compiler plan stage) in parallel, with
    within-run deduplication, per-tool call-count caps, semantic redundancy
    suppression, and billing accumulation (billable_tool_calls,
    billable_tool_names, delegation_spend_usdc).
  * ``_execute_single_tool`` / ``_execute_single_tool_safe`` — per-call
    execution with per-tool timeout and failure isolation.
  * ``_call_signature`` — stable dedup hash for a (tool_name, args) pair.
  * ``_get_liquidation_risk_targets`` — wallet+chain keys for semantic
    redundancy checks against get_defi_positions coverage.

The platform tool cache (``_get_cached_tools_by_name``) lives in ``agent.nodes``
and is resolved lazily at call time so that tests patching the cache on
``agent.nodes`` continue to take effect.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from agent.node_usage import _covered_defi_keys_from_result
from agent.planner_ir import resolve_plan_references
from agent.slots import summarize_into_slots
from agent.state import AgentState, TaskStatus
from teardrop.config import get_settings
from tools.executor import execute_tool

logger = logging.getLogger(__name__)


def _get_platform_tools_by_name() -> dict:
    """Resolve the platform tool cache via ``agent.nodes`` at call time.

    The cache and its accessor live in ``agent.nodes``; resolving it lazily keeps
    a single source of truth and lets tests that patch the cache (or its accessor)
    on ``agent.nodes`` take effect inside ``tool_executor_node``.
    """
    from agent import nodes as _nodes

    return _nodes._get_cached_tools_by_name()


def _get_liquidation_risk_targets(call_args: dict[str, Any]) -> set[str]:
    """Build wallet+chain keys targeted by a get_liquidation_risk call."""
    chain_id = call_args.get("chain_id")
    wallets = call_args.get("wallet_addresses") or []
    if chain_id is None or not isinstance(wallets, list):
        return set()
    return {f"{int(chain_id)}:{str(addr).lower()}" for addr in wallets if addr}


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
        from teardrop.config import get_settings as _get_settings

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
            f"[TOOL_ERROR] '{tool_name}' failed unexpectedly ({type(exc).__name__}). Continue synthesis with available data."
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


# NOTE: `config` is intentionally left UNANNOTATED. This module uses
# `from __future__ import annotations` (PEP 563), which stringifies type hints.
# LangGraph 1.x detects the config-injection parameter by inspecting the raw
# annotation object; a stringified `RunnableConfig` hint is NOT recognized, so
# the runtime config (e.g. `_org_tools_by_name`) would silently fail to inject
# and `config` would stay `None`. Leaving it unannotated forces name-based injection.
async def tool_executor_node(state: AgentState, config=None) -> dict[str, Any]:
    """Execute all pending tool calls from the latest AIMessage in parallel.

    Resolves tool calls from the compiler plan (if active) or from
    ``state.messages[-1].tool_calls``. Applies within-run deduplication
    (identical tool_name + args are suppressed to prevent re-fetching
    already-present context). Enforces per-tool call-count limits
    (``agent_tool_max_calls_per_run``) and the ``max_custom_tool_calls_per_run`` quota.

    Billing accumulators updated in ``_usage``:
      * ``billable_tool_calls`` — successful non-failed calls (used for cost accounting)
      * ``billable_tool_names`` — tool names that ran (used for marketplace earnings
        recording and the USAGE_SUMMARY SSE event)
      * ``failed_tool_calls`` / ``failed_tool_names`` — for diagnostics
      * ``delegation_spend_usdc`` — accumulated cost from ``delegate_to_agent`` calls

    Reads per-call results into ``state.slots`` via ``summarize_into_slots``.
    Returns a state patch containing tool result messages and updated metadata.
    """
    logger.debug("tool_executor_node: entry")
    plan = state.plan
    plan_outputs = dict(state.metadata.get("_plan_outputs", {}))
    using_plan = bool(plan is not None and not plan.is_done())

    incoming_calls: list[dict[str, Any]] = []
    stage_call_ids: list[str] = []
    if using_plan and plan is not None:
        stage = plan.stages[plan.current_stage_index]
        completed = set(plan.completed_call_ids)
        for call in stage.calls:
            if any(dep not in completed for dep in call.depends_on):
                continue
            resolved_args = resolve_plan_references(call.args, plan_outputs)
            incoming_calls.append({"id": call.call_id, "name": call.tool, "args": resolved_args})
            stage_call_ids.append(call.call_id)
    else:
        last_msg = state.messages[-1]
        if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
            return {"task_status": TaskStatus.GENERATING_UI}
        incoming_calls = list(last_msg.tool_calls)
        stage_call_ids = [str(c.get("id", "")) for c in incoming_calls if isinstance(c, dict)]

    _configurable = (config or {}).get("configurable", {})
    platform_tools_by_name = _get_platform_tools_by_name()
    tools_by_name = {
        **platform_tools_by_name,
        **(_configurable.get("_org_tools_by_name") or state.metadata.get("_org_tools_by_name", {})),
    }
    excluded_tool_names = frozenset(state.metadata.get("_excluded_tool_names", []))
    if excluded_tool_names:
        tools_by_name = {name: tool for name, tool in tools_by_name.items() if name not in excluded_tool_names}
    platform_tool_names = set(platform_tools_by_name.keys())

    # ── Accumulate tool usage ─────────────────────────────────────────────
    usage = dict(state.metadata.get("_usage", {}))
    tool_names_acc: list[str] = list(usage.get("tool_names", []))
    failed_tool_names_acc: list[str] = list(usage.get("failed_tool_names", []))
    tool_call_counts: dict[str, int] = dict(usage.get("_tool_call_counts", {}))
    custom_tool_calls = int(usage.get("custom_tool_calls", 0))
    defi_positions_covered: set[str] = set(usage.get("_defi_positions_covered", []))
    tool_max_calls = dict(get_settings().agent_tool_max_calls_per_run)
    max_custom_tool_calls = int(get_settings().max_custom_tool_calls_per_run)
    incoming_call_names = [str(call.get("name", "")) for call in incoming_calls if isinstance(call, dict)]

    logger.debug(
        (
            "tool_executor: inventory incoming_calls=%s available_platform_tools=%s "
            "available_org_tools=%s excluded=%s custom_tool_calls=%d max_custom_tool_calls=%d"
        ),
        incoming_call_names,
        sorted(platform_tool_names),
        sorted(name for name in tools_by_name if name not in platform_tool_names),
        sorted(excluded_tool_names) if excluded_tool_names else [],
        custom_tool_calls,
        max_custom_tool_calls,
    )

    # ── Within-run deduplication ──────────────────────────────────────────
    # Calls with identical (tool_name, args) are suppressed so a re-entering
    # planner cannot re-fetch data that is already in the conversation.
    completed_sigs: list[str] = list(usage.get("_completed_calls", []))
    dedup_calls: list[dict] = []
    skipped_messages: list[ToolMessage] = []
    skipped_reason_codes: list[str] = []
    seen_this_batch: set[str] = set()
    for call in incoming_calls:
        # Pre-empt unknown/unbound tool calls before any other processing.
        if call["name"] not in tools_by_name:
            skipped_reason_codes.append(f"{call['name']}:tool_unavailable")
            skipped_messages.append(
                ToolMessage(
                    content=(
                        f"[TOOL_UNAVAILABLE] Tool '{call['name']}' is not configured in this run. Synthesize from existing data."
                    ),
                    tool_call_id=call["id"],
                )
            )
            continue
        if call["name"] not in platform_tool_names:
            accepted_custom_in_batch = sum(1 for c in dedup_calls if c["name"] not in platform_tool_names)
            if (custom_tool_calls + accepted_custom_in_batch) >= max_custom_tool_calls:
                skipped_reason_codes.append(f"{call['name']}:custom_tool_cap")
                skipped_messages.append(
                    ToolMessage(
                        content=(f"[CUSTOM_TOOL_CAP_EXCEEDED] custom tool calls exceeded per-run cap ({max_custom_tool_calls})."),
                        tool_call_id=call["id"],
                    )
                )
                continue
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
            skipped_reason_codes.append(f"{call['name']}:tool_cap")
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
                skipped_reason_codes.append(f"{call['name']}:semantic_redundancy")
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
                    skipped_reason_codes.append(f"{call['name']}:address_only")
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
            skipped_reason_codes.append(f"{call['name']}:duplicate")
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

    logger.debug(
        "tool_executor: resolution accepted_calls=%s skipped=%s",
        [str(call.get("name", "")) for call in dedup_calls if isinstance(call, dict)],
        skipped_reason_codes,
    )

    if not dedup_calls:
        usage["tool_iterations"] = usage.get("tool_iterations", 0) + 1
        if using_plan and plan is not None:
            for call_id in stage_call_ids:
                if call_id and call_id not in plan.completed_call_ids:
                    plan.completed_call_ids.append(call_id)
            plan.current_stage_index += 1
            logger.info("tool_executor: plan stage had no executable calls, advancing stage")
            return {
                "messages": skipped_messages,
                "task_status": TaskStatus.PLANNING,
                "metadata": {
                    **state.metadata,
                    "_usage": usage,
                    "_synthesis_forced": False,
                    "_plan_outputs": plan_outputs,
                },
                "slots": state.slots,
                "plan": plan,
            }

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
    usage["custom_tool_calls"] = custom_tool_calls + sum(1 for c in dedup_calls if c["name"] not in platform_tool_names)
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

    slots = dict(state.slots)
    for res in results:
        slots = summarize_into_slots(res["name"], str(res.get("content", "")), slots)

    next_plan = plan
    if using_plan and plan is not None:
        for call_id in stage_call_ids:
            if call_id and call_id not in plan.completed_call_ids:
                plan.completed_call_ids.append(call_id)

        for call, res in zip(dedup_calls, results):
            raw = res.get("content", "")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw
            plan_outputs[str(call.get("id", ""))] = parsed

        plan.current_stage_index += 1
        next_plan = plan

    return {
        "messages": tool_messages,
        "task_status": TaskStatus.PLANNING,
        "metadata": {**state.metadata, "_usage": usage, "_plan_outputs": plan_outputs},
        "slots": slots,
        "plan": next_plan,
    }
