# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Usage accounting and DeFi coverage helpers for the Teardrop agent nodes.

These pure helpers are factored out of ``agent.nodes`` for semantic clarity and
focused testing. They are re-exported from ``agent.nodes`` for backward
compatibility, so existing imports of ``agent.nodes._accumulate_usage`` and
``agent.nodes._covered_defi_keys_from_result`` continue to work.

  * ``_accumulate_usage`` — folds one LLM turn's token counts (tokens_in,
    tokens_out, cache_read_tokens, cache_creation_tokens) into the running
    ``_usage`` dict and appends a per-turn attribution record. Drives the
    USAGE_SUMMARY SSE event and run-cost settlement.
  * ``_covered_defi_keys_from_result`` — derives wallet+chain keys that have
    full DeFi risk coverage (Aave + Compound) from a get_defi_positions result,
    used to decide whether get_liquidation_risk may be suppressed.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage

from agent.llm import extract_usage
from agent.state import AgentState


def _accumulate_usage(state: AgentState, response: AIMessage, *, provider: str, model: str) -> dict[str, Any]:
    """Add this turn's token counts to running usage and keep per-turn attribution."""
    usage = dict(state.metadata.get("_usage", {}))
    extracted = extract_usage(response)
    delta_in = int(extracted.get("tokens_in", 0))
    delta_out = int(extracted.get("tokens_out", 0))
    delta_cache_read = int(extracted.get("cache_read_input_tokens", 0))
    delta_cache_creation = int(extracted.get("cache_creation_input_tokens", 0))
    usage["tokens_in"] = int(usage.get("tokens_in", 0)) + delta_in
    usage["tokens_out"] = int(usage.get("tokens_out", 0)) + delta_out
    usage["cache_read_tokens"] = int(usage.get("cache_read_tokens", 0)) + delta_cache_read
    usage["cache_creation_tokens"] = int(usage.get("cache_creation_tokens", 0)) + delta_cache_creation

    turns = usage.get("turns")
    if not isinstance(turns, list):
        turns = []
    turns.append(
        {
            "provider": str(provider or ""),
            "model": str(model or ""),
            "tokens_in": delta_in,
            "tokens_out": delta_out,
            "cache_read_tokens": delta_cache_read,
            "cache_creation_tokens": delta_cache_creation,
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
