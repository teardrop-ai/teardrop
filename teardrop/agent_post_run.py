# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Post-run usage accounting and settlement for the agent run endpoint.

Extracted verbatim from ``teardrop.routers.agent``'s inner ``_stream``
generator. These helpers run after the LangGraph event loop completes and never
block the SSE stream:

* :func:`fetch_usage_snapshot` reads the final graph state (best-effort).
* :func:`calculate_run_cost` derives the atomic-USDC run cost from live pricing.
* :func:`dispatch_settlement` performs credit debit or x402 on-chain settlement
  and yields ``BILLING_SETTLEMENT`` SSE frames, signalling whether marketplace
  stats should be billed via the supplied ``result`` dict.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from billing import (
    calculate_byok_orchestration_cost,
    calculate_run_cost_usdc,
    debit_credit,
    enqueue_failed_settlement,
    record_settlement,
    settle_payment,
    verify_settlement_on_chain,
)
from teardrop.agent_stream import _EV_BILLING_SETTLEMENT, _sse_event
from teardrop.agent_telemetry import _log_agent_memory
from teardrop.memory import extract_and_store_memories
from teardrop.usage import record_tool_call_events

logger = logging.getLogger(__name__)


def record_post_run_telemetry(
    *,
    run_id: str,
    org_id: str,
    user_id: str,
    usage_data: dict[str, Any],
    state_values: dict[str, Any] | None,
    settings: Any,
    outcome: int = 0,
    outcome_source: str = "",
) -> None:
    """Schedule best-effort ML telemetry without delaying a completed run."""
    if settings.tool_call_event_logging_enabled:
        tool_call_log = usage_data.get("_tool_call_log", [])
        if isinstance(tool_call_log, list) and tool_call_log:
            asyncio.create_task(record_tool_call_events(run_id, org_id, tool_call_log))

    if not settings.memory_enabled or not state_values:
        return

    try:
        state_messages = state_values.get("messages", [])[-10:]
        if not state_messages:
            return
        billable_tool_names = usage_data.get("billable_tool_names", usage_data.get("tool_names", []))
        if not isinstance(billable_tool_names, list):
            billable_tool_names = []
        run_slots = state_values.get("slots", {})
        asyncio.create_task(
            extract_and_store_memories(
                org_id,
                user_id,
                state_messages,
                run_id,
                tool_names_used=[str(name) for name in billable_tool_names],
                slots=run_slots if isinstance(run_slots, dict) else {},
                outcome=outcome,
                outcome_source=outcome_source,
            )
        )
    except Exception:
        logger.debug("Post-run memory telemetry kickoff failed", exc_info=True)


async def fetch_usage_snapshot(
    *,
    graph: Any,
    config: dict[str, Any],
    run_id: str,
    settings: Any,
) -> tuple[Any, dict[str, Any]]:
    """Best-effort read of the final graph state and its ``_usage`` metadata."""
    usage_data: dict[str, Any] = {}
    # state_snapshot is also read later by the memory-extraction kickoff;
    # initialise to None so a timeout/exception leaves it well-defined.
    state_snapshot = None
    state_started = time.monotonic()
    _log_agent_memory("aget_state_start", run_id=run_id)
    try:
        state_snapshot = await asyncio.wait_for(
            graph.aget_state(config),
            timeout=settings.agent_state_snapshot_timeout_seconds,
        )
        usage_data = (state_snapshot.values or {}).get("metadata", {}).get("_usage", {})
    except asyncio.TimeoutError:
        logger.warning(
            "agent_run aget_state timed out after %.1fs run_id=%s; skipping usage data",
            settings.agent_state_snapshot_timeout_seconds,
            run_id,
        )
    except Exception:
        logger.debug("Could not retrieve final state for usage", exc_info=True)
    finally:
        _log_agent_memory(
            "aget_state_end",
            run_id=run_id,
            elapsed_ms=int((time.monotonic() - state_started) * 1000),
        )
    return state_snapshot, usage_data


async def calculate_run_cost(
    *,
    usage_data: dict[str, Any],
    llm_config: dict[str, Any] | None,
    settings: Any,
) -> int:
    """Calculate usage-based cost from live pricing rule (never blocks the stream)."""
    cost_usdc = 0
    try:
        _run_provider = llm_config["provider"] if llm_config else settings.agent_provider
        _run_model = llm_config["model"] if llm_config else settings.agent_model
        turns = usage_data.get("turns") if isinstance(usage_data, dict) else None
        if isinstance(turns, list) and turns:
            token_cost_total = 0
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                turn_provider = str(turn.get("provider") or _run_provider)
                turn_model = str(turn.get("model") or _run_model)
                turn_usage = {
                    "tokens_in": int(turn.get("tokens_in", 0)),
                    "tokens_out": int(turn.get("tokens_out", 0)),
                    # Token-only per turn; tools are charged separately once per run.
                    "billable_tool_calls": 0,
                    "billable_tool_names": [],
                }
                token_cost_total += await calculate_run_cost_usdc(turn_usage, turn_provider, turn_model)

            tool_usage = {
                "tokens_in": 0,
                "tokens_out": 0,
                "billable_tool_calls": int(usage_data.get("billable_tool_calls", usage_data.get("tool_calls", 0))),
                "billable_tool_names": usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
            }
            tool_cost_total = await calculate_run_cost_usdc(tool_usage, _run_provider, _run_model)
            cost_usdc = token_cost_total + tool_cost_total
        else:
            cost_usdc = await calculate_run_cost_usdc(usage_data, _run_provider, _run_model)
    except Exception:
        logger.debug("Could not calculate run cost", exc_info=True)
    return cost_usdc


async def dispatch_settlement(
    *,
    billing: Any,
    is_byok: bool,
    settings: Any,
    org_llm_cfg: Any,
    usage_data: dict[str, Any],
    usage_event: Any,
    platform_fee: int,
    cost_usdc: int,
    delegation_spend: int,
    org_id: Any,
    run_id: str,
    result: dict[str, Any],
):
    """Credit debit or x402 settlement, yielding ``BILLING_SETTLEMENT`` frames.

    Sets ``result["marketplace_stats_billable"]`` to ``True`` when a charge
    succeeded so the caller can record marketplace tool usage stats.
    """
    result["marketplace_stats_billable"] = False

    if not billing.verified:
        return

    # Determine what to charge BYOK orgs.
    # - byok_tier_pricing_enabled=True (migration 041 applied): per-token
    #   orchestration cost floored at byok_platform_fee_usdc.
    # - byok_tier_pricing_enabled=False (legacy / pre-migration): flat fee.
    # Non-BYOK orgs always pay the full LLM cost.
    if is_byok and settings.byok_tier_pricing_enabled:
        _run_provider = (org_llm_cfg.provider if org_llm_cfg else "") or ""
        _run_model = (org_llm_cfg.model if org_llm_cfg else "") or ""
        debit_amount = await calculate_byok_orchestration_cost(
            usage_data.get("tokens_in", 0),
            usage_data.get("tokens_out", 0),
            provider=_run_provider,
            model=_run_model,
        )
    else:
        # Legacy: flat fee for BYOK, full model cost for non-BYOK.
        debit_amount = platform_fee if is_byok else cost_usdc

    if billing.billing_method == "credit":
        # Debit actual run cost (or platform fee for BYOK) from org's prepaid balance.
        success, deducted_amount = await debit_credit(org_id, debit_amount, reason=f"run:{run_id}")
        if success:
            result["marketplace_stats_billable"] = True
            await record_settlement(usage_event.id, deducted_amount, "", "settled")
            yield _sse_event(
                _EV_BILLING_SETTLEMENT,
                {
                    "run_id": run_id,
                    "amount_usdc": deducted_amount,
                    "tx_hash": "",
                    "network": "credit",
                    "delegation_cost_usdc": delegation_spend,
                    "platform_fee_usdc": platform_fee,
                },
            )
        else:
            await record_settlement(usage_event.id, debit_amount, "", "failed")
            await enqueue_failed_settlement(
                usage_event.id,
                org_id,
                run_id,
                "credit",
                debit_amount,
            )
            logger.warning("Credit debit failed run_id=%s org_id=%s", run_id, org_id)
    else:
        # x402 on-chain settlement.
        # Clamp to the upto ceiling the client signed; otherwise settlement
        # would fail because the signed amount cannot cover the higher cost.
        if settings.x402_scheme == "upto":
            upto_ceiling = settings.x402_upto_max_amount_atomic
            if upto_ceiling > 0 and cost_usdc > upto_ceiling:
                logger.warning(
                    "Run cost exceeds x402 upto ceiling; clamping run_id=%s org_id=%s cost_usdc=%d ceiling_usdc=%d",
                    run_id,
                    org_id,
                    cost_usdc,
                    upto_ceiling,
                )
                cost_usdc = upto_ceiling
        # Hard timeout on the facilitator HTTP call so a slow/unreachable
        # facilitator can never hold the SSE stream open indefinitely.
        # On timeout we route to the same failure path used when the
        # facilitator returns a non-success response: enqueue for retry
        # by the background worker (see process_pending_settlements).
        settlement_timed_out = False
        try:
            billing_settled = await asyncio.wait_for(
                settle_payment(billing, actual_cost_usdc=cost_usdc),
                timeout=settings.x402_settlement_timeout_seconds,
            )
        except asyncio.TimeoutError:
            settlement_timed_out = True
            billing_settled = billing  # placeholder; settled=False by default
            logger.warning(
                "Settlement timed out after %ds run_id=%s org_id=%s; enqueued for retry",
                settings.x402_settlement_timeout_seconds,
                run_id,
                org_id,
            )
        if not settlement_timed_out and billing_settled.settled:
            result["marketplace_stats_billable"] = True
            await record_settlement(
                usage_event.id,
                billing_settled.amount_usdc,
                billing_settled.tx_hash,
                "settled",
            )
            yield _sse_event(
                _EV_BILLING_SETTLEMENT,
                {
                    "run_id": run_id,
                    "amount_usdc": billing_settled.amount_usdc,
                    "tx_hash": billing_settled.tx_hash,
                    "network": settings.x402_network,
                    "delegation_cost_usdc": delegation_spend,
                    "platform_fee_usdc": platform_fee,
                },
            )
            if billing_settled.tx_hash:
                try:
                    chain_id = int(str(settings.x402_network).rsplit(":", 1)[-1])
                except (TypeError, ValueError):
                    logger.warning(
                        "Skipping x402 receipt check due to unparseable network: %s",
                        settings.x402_network,
                    )
                else:
                    asyncio.create_task(
                        verify_settlement_on_chain(
                            usage_event.id,
                            billing_settled.tx_hash,
                            chain_id,
                        )
                    )
        else:
            await record_settlement(usage_event.id, 0, "", "failed")
            await enqueue_failed_settlement(
                usage_event.id,
                org_id,
                run_id,
                "x402",
                cost_usdc,
                payment_payload=str(billing.payment_payload) if billing.payment_payload else None,
            )
            logger.warning(
                "Settlement failed run_id=%s: %s",
                run_id,
                billing_settled.error,
            )
