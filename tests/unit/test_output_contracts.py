# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage

import agent.nodes as nodes_module
from agent.output_contracts import (
    ETH_PRIMITIVE_FEES_TASK,
    build_eth_primitive_fallback,
    detect_output_contract,
    get_output_contract,
    normalize_eth_primitive_output,
)
from agent.runtime_context import AgentRunContext, agent_run_context
from agent.state import AgentState, TaskStatus

_SLUGS = ["liquity", "railgun", "tornado-cash", "aave-v3", "uniswap", "lido", "makerdao"]


def _valid_report() -> dict:
    return {
        "task_class": ETH_PRIMITIVE_FEES_TASK,
        "schema_version": 1,
        "generated_at": "2026-08-15T00:00:00Z",
        "data_gaps": [],
        "label_definition": {
            "next_week_fee_direction": "up/down/flat",
            "cheap_expensive": "basket median",
        },
        "basket_median_fee_to_fdv": 0.01,
        "protocols": [
            {
                "slug": slug,
                "fees_7d_change_pct": 1.0,
                "fees_30d_change_pct": 2.0,
                "revenue_7d_change_pct": 1.0,
                "revenue_30d_change_pct": 2.0,
                "tvl_7d_change_pct": 1.0,
                "fee_to_fdv": 0.01,
                "price_24h_change_pct": 1.0,
                "data_gaps": [],
                "prediction": {
                    "next_week_fee_direction": "flat",
                    "confidence": 0.9,
                    "rationale": "Observed fees were within the flat threshold.",
                    "prediction_outcome": None,
                },
                "staking_link": {
                    "mechanism": "Protocol-specific fee flow.",
                    "fee_trend_supports_yield": True,
                    "reason": "Observed fees were stable.",
                },
                "valuation_signal": {
                    "cheap_expensive": "neutral",
                    "reason": "The ratio is near the basket median.",
                },
            }
            for slug in _SLUGS
        ],
        "basket_summary": {
            "fees_30d_change_pct": 2.0,
            "health_direction": "improving",
            "top_signal": "The basket showed a small positive fee trend.",
        },
    }


def test_detect_output_contract_uses_latest_human_turn():
    messages = [
        HumanMessage(content="TASK_CLASS: eth_primitive_fees"),
        HumanMessage(content="Give me a plain answer."),
    ]

    assert detect_output_contract(messages) is None
    contract = detect_output_contract([messages[0]])
    assert contract is not None
    assert contract.task_class == ETH_PRIMITIVE_FEES_TASK


def test_registry_resolves_all_scheduled_contracts():
    for task_class in ("eth_primitive_fees", "entry_timing", "stablecoin_yield_compare"):
        contract = get_output_contract(task_class)
        assert contract is not None
        assert contract.schema_version == 1


def test_detect_output_contract_resolves_all_production_task_classes():
    for task_class in ("eth_primitive_fees", "entry_timing", "stablecoin_yield_compare"):
        contract = detect_output_contract([HumanMessage(content=f"TASK_CLASS: {task_class}")])
        assert contract is not None
        assert contract.task_class == task_class


def test_scheduled_contract_schemas_accept_null_safe_records_and_reject_extras():
    entry_contract = get_output_contract("entry_timing")
    stable_contract = get_output_contract("stablecoin_yield_compare")
    assert entry_contract is not None
    assert stable_contract is not None

    token_ids = [
        "proton-loan",
        "gains-network",
        "nosana",
        "liquity",
        "chain-2",
        "tornado-cash",
        "railgun",
        "aixbt",
        "venice-token",
        "orca",
    ]
    entry_payload = {
        "task_class": "entry_timing",
        "schema_version": 1,
        "generated_at": None,
        "data_gaps": [],
        "tokens": [
            {
                "id": token_id,
                "has_staking": 0,
                "current_price_usd": None,
                "drawdown_pct": None,
                "volatility": None,
                "dca_baseline_price_usd": None,
                "signal_vs_dca": None,
                "signal": None,
                "entry_price_usd": None,
                "stop_price_usd": None,
                "volume_24h": None,
                "market_cap_usd": None,
                "confidence": 0.5,
                "entry_outcome": None,
                "data_gaps": ["price"],
            }
            for token_id in token_ids
        ],
        "actionable": [],
        "executive_summary": "No actionable entry this week.",
        "summary": {"entry_candidates": [], "risk_ranking": token_ids, "notable_shift": "no comparable prior snapshot"},
    }
    assert entry_contract.validate(entry_payload)
    entry_payload["unexpected"] = True
    assert not entry_contract.validate(entry_payload)

    symbols = [
        "LUSD",
        "BOLD",
        "crvUSD",
        "GHO",
        "DAI",
        "USDT",
        "USDC",
        "PYUSD",
        "RLUSD",
        "USDG",
        "USD1",
        "FDUSD",
        "TUSD",
        "USDP",
        "USDS",
    ]
    stable_payload = {
        "task_class": "stablecoin_yield_compare",
        "schema_version": 1,
        "generated_at": None,
        "data_gaps": ["yield", "price"],
        "stables": [
            {
                "symbol": symbol,
                "basket": "decentralized" if symbol in {"LUSD", "BOLD", "crvUSD", "GHO", "DAI"} else "centralized",
                "best_apy_pct": None,
                "best_protocol": None,
                "pool_tvl_usd": None,
                "apy_base_pct": None,
                "apy_reward_pct": None,
                "reward_dominated": None,
                "price_usd": None,
                "market_cap_usd": None,
                "peg_deviation_bps": None,
                "peg_deviation": None,
                "data_gaps": ["yield", "price"],
            }
            for symbol in symbols
        ],
        "executive_summary": "Insufficient data.",
        "basket_metrics": {
            "decentralized_avg_apy_pct": None,
            "centralized_avg_apy_pct": None,
            "spread_pct": None,
            "top_yield_decentralized": None,
            "top_yield_centralized": None,
            "decentralized_peg_health": None,
            "centralized_peg_health": None,
            "decentralized_reward_dominated": None,
            "centralized_reward_dominated": None,
        },
        "spread": None,
        "winner": None,
        "prediction": {
            "next_week_spread_direction": None,
            "next_week_basket_winner": None,
            "confidence": 0.0,
            "rationale": "No valid PREVIOUS_WEEK snapshot.",
            "spread_outcome": None,
        },
        "market_analysis": {
            "yield_regime": "insufficient_data",
            "peg_stress": "insufficient_data",
            "notable_shift": "no comparable prior snapshot",
        },
    }
    assert stable_contract.validate(stable_payload)


def test_normalize_output_canonicalizes_json_and_preserves_summary():
    raw = "Reasoning before output\n" + json.dumps(_valid_report()) + "\n---\nHealth: improving"

    normalized = normalize_eth_primitive_output(raw)

    assert normalized is not None
    json_part, summary = normalized.split("\n---\n", maxsplit=1)
    assert json.loads(json_part)["task_class"] == ETH_PRIMITIVE_FEES_TASK
    assert summary == "Health: improving"
    assert normalized.startswith("{")


def test_normalize_output_rejects_incomplete_basket():
    payload = _valid_report()
    payload["protocols"] = payload["protocols"][:-1]

    assert normalize_eth_primitive_output(json.dumps(payload)) is None


def test_normalize_output_accepts_prompt_example_shape_with_null_numerics_and_gaps():
    """Pins the corrected scheduled-task prompt shape: numeric fields are null
    (never the literal "DATA_GAP" string) with field names in per-protocol
    data_gaps, and prediction carries the schema-required prediction_outcome."""
    payload = _valid_report()
    for protocol in payload["protocols"]:
        protocol["fees_7d_change_pct"] = None
        protocol["fees_30d_change_pct"] = None
        protocol["revenue_7d_change_pct"] = None
        protocol["revenue_30d_change_pct"] = None
        protocol["tvl_7d_change_pct"] = None
        protocol["fee_to_fdv"] = None
        protocol["price_24h_change_pct"] = None
        protocol["data_gaps"] = ["fees", "fdv"]
        protocol["prediction"]["confidence"] = 0.0
    payload["basket_median_fee_to_fdv"] = None

    normalized = normalize_eth_primitive_output(json.dumps(payload))

    assert normalized is not None
    canonical = json.loads(normalized.split("\n---\n", maxsplit=1)[0])
    assert canonical["protocols"][0]["fees_7d_change_pct"] is None
    assert canonical["protocols"][0]["data_gaps"] == ["fees", "fdv"]


def test_normalize_output_rejects_data_gap_string_in_numeric_field():
    """The old prompt wording ('record it as DATA_GAP') produced a string in a
    ["number", "null"] field, which must fail validation."""
    payload = _valid_report()
    payload["protocols"][0]["fees_7d_change_pct"] = "DATA_GAP"
    payload["protocols"][0]["data_gaps"] = ["fees"]

    assert normalize_eth_primitive_output(json.dumps(payload)) is None


def test_normalize_output_rejects_missing_prediction_outcome():
    """The original prompt example omitted the schema-required prediction_outcome;
    this pin ensures the corrected example shape stays valid and the old one fails."""
    contract = get_output_contract(ETH_PRIMITIVE_FEES_TASK)
    assert contract is not None

    valid_payload = _valid_report()
    assert contract.validate(valid_payload)

    stale_prompt_example = _valid_report()
    del stale_prompt_example["protocols"][0]["prediction"]["prediction_outcome"]
    assert not contract.validate(stale_prompt_example)


def test_fallback_is_machine_parseable_and_explicitly_insufficient():
    fallback = build_eth_primitive_fallback()
    json_part, summary = fallback.split("\n---\n", maxsplit=1)
    payload = json.loads(json_part)

    assert payload["task_class"] == ETH_PRIMITIVE_FEES_TASK
    assert payload["schema_version"] == 1
    assert payload["generated_at"]
    assert payload["contract_status"] == "validation_failed"
    assert payload["data_gaps"] == ["OUTPUT_CONTRACT_VALIDATION_FAILED"]
    assert summary == "No labels emitted because the output contract could not be validated."


async def test_planner_repairs_invalid_contract_once(test_settings):
    invalid_response = AIMessage(content="I have all the data I need.")
    repaired_response = AIMessage(content=json.dumps(_valid_report()))
    invoke = AsyncMock(side_effect=[invalid_response, repaired_response])
    state = AgentState(
        messages=[HumanMessage(content="TASK_CLASS: eth_primitive_fees")],
        metadata={"_usage": {}},
    )

    with (
        agent_run_context(AgentRunContext([], {})),
        patch.object(nodes_module, "_cached_tools", []),
        patch.object(nodes_module, "_resolve_planner_llm", return_value=(MagicMock(), "google", "test-model", 100, 5, None)),
        patch.object(nodes_module, "_invoke_planner_llm", invoke),
    ):
        result = await nodes_module.planner_node(state)

    assert invoke.await_count == 2
    assert result["task_status"] == TaskStatus.GENERATING_UI
    assert result["metadata"]["_output_contract_active"] is True
    assert result["metadata"]["_output_contract_ok"] is True
    assert json.loads(result["messages"][0].content.split("\n---\n", maxsplit=1)[0])["task_class"] == ETH_PRIMITIVE_FEES_TASK


async def test_planner_uses_failure_envelope_when_repair_is_provider_error(test_settings):
    """If the repair call itself fails at the provider level, the response must be
    the failure envelope, not the original prose."""
    invalid_response = AIMessage(content="I have all the data I need.")
    provider_error = {
        "messages": [AIMessage(content="The AI model timed out. Please try again.")],
        "task_status": TaskStatus.FAILED,
    }
    invoke = AsyncMock(side_effect=[invalid_response, provider_error])
    state = AgentState(
        messages=[HumanMessage(content="TASK_CLASS: eth_primitive_fees")],
        metadata={"_usage": {}},
    )

    with (
        agent_run_context(AgentRunContext([], {})),
        patch.object(nodes_module, "_cached_tools", []),
        patch.object(nodes_module, "_resolve_planner_llm", return_value=(MagicMock(), "google", "test-model", 100, 5, None)),
        patch.object(nodes_module, "_invoke_planner_llm", invoke),
    ):
        result = await nodes_module.planner_node(state)

    assert invoke.await_count == 2
    assert result["task_status"] == TaskStatus.GENERATING_UI
    assert result["metadata"]["_output_contract_active"] is True
    assert result["metadata"]["_output_contract_ok"] is False
    assert json.loads(result["messages"][0].content.split("\n---\n", maxsplit=1)[0])["contract_status"] == "validation_failed"
