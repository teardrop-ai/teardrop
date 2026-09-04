# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from agent.slots import render_slots_markdown, summarize_into_slots


def test_summarize_wallet_portfolio_into_slots():
    payload = (
        '{"wallet_address":"0xabc","chain_id":1,"holdings":['
        '{"symbol":"ETH","balance_formatted":"1.0","value_usd":3000,"price_usd":3000}]}'
    )
    slots = summarize_into_slots("get_wallet_portfolio", payload, {})
    assert "balances" in slots
    assert "1:0xabc" in slots["balances"]
    assert slots["balances"]["1:0xabc"]["ETH"]["balance_formatted"] == "1.0"


def test_render_slots_markdown_deterministic():
    slots = {
        "prices": {"by_symbol": {"ETH": {"price": 3000}}},
        "balances": {"1:0xabc": {"ETH": {"balance_formatted": "1.0"}}},
    }
    text = render_slots_markdown(slots)
    assert text.startswith("## Known Facts")
    assert "balances.1:0xabc" in text
    assert "prices.by_symbol" in text


def test_unknown_tool_does_not_change_slots():
    slots = {"a": 1}
    out = summarize_into_slots("unknown", "{}", slots)
    assert out == slots


def test_summarize_protocol_tvl_into_slots():
    payload = (
        '{"protocol":"aave","current_tvl_usd":12345.67,'
        '"tvl_7d_change_pct":1.25,"tvl_30d_change_pct":-3.5,'
        '"current_fees_usd":100.0,"fees_7d_change_pct":2.5,"fees_30d_change_pct":-1.5,'
        '"current_revenue_usd":50.0,"revenue_7d_change_pct":1.5,"revenue_30d_change_pct":-0.5,'
        '"revenue_error_type":"upstream_error",'
        '"chain_breakdown":[{"chain":"Ethereum","tvl_usd":1000}],'
        '"historical_series":[{"date":"2026-05-01","tvl_usd":1000}],'
        '"note":"ok"}'
    )
    slots = summarize_into_slots("get_protocol_tvl", payload, {})
    assert "tvl" in slots
    assert "aave" in slots["tvl"]
    assert slots["tvl"]["aave"]["current_tvl_usd"] == 12345.67
    assert slots["tvl"]["aave"]["tvl_7d_change_pct"] == 1.25
    assert slots["tvl"]["aave"]["tvl_30d_change_pct"] == -3.5
    assert slots["tvl"]["aave"]["current_fees_usd"] == 100.0
    assert slots["tvl"]["aave"]["fees_7d_change_pct"] == 2.5
    assert slots["tvl"]["aave"]["current_revenue_usd"] == 50.0
    assert slots["tvl"]["aave"]["revenue_error_type"] == "upstream_error"
    assert slots["tvl"]["aave"]["note"] == "ok"
    assert "chain_breakdown" not in slots["tvl"]["aave"]
    assert "historical_series" not in slots["tvl"]["aave"]


def test_summarize_protocol_tvl_batch_list():
    """Batch get_protocol_tvl result (list of dicts) merges into slots."""
    import json

    payload = json.dumps(
        [
            {
                "protocol": "aave-v3",
                "current_tvl_usd": 12345.0,
                "tvl_7d_change_pct": 1.5,
                "tvl_30d_change_pct": -2.0,
                "note": "ok",
            },
            {
                "protocol": "uniswap-v3",
                "current_tvl_usd": 6789.0,
                "tvl_7d_change_pct": 0.5,
                "tvl_30d_change_pct": 1.0,
                "note": "ok",
            },
        ]
    )
    slots = summarize_into_slots("get_protocol_tvl", payload, {})
    assert "tvl" in slots
    assert slots["tvl"]["aave-v3"]["current_tvl_usd"] == 12345.0
    assert slots["tvl"]["uniswap-v3"]["current_tvl_usd"] == 6789.0


def test_summarize_protocol_tvl_empty_list():
    """Empty list returns slots unchanged."""
    slots = summarize_into_slots("get_protocol_tvl", "[]", {"tvl": {}})
    assert slots == {"tvl": {}}


def test_summarize_protocol_tvl_list_with_error_item():
    """List item with empty protocol is skipped without error."""
    import json

    slots = summarize_into_slots(
        "get_protocol_tvl",
        json.dumps([{"protocol": "", "error": "not found"}]),
        {},
    )
    assert slots == {}


def test_summarize_ignores_non_dict_list_items():
    """Non-dict items in a list are ignored; only dict items processed."""
    import json

    payload = json.dumps(["string", 42, {"protocol": "aave-v3", "current_tvl_usd": 100.0}])
    slots = summarize_into_slots("get_protocol_tvl", payload, {})
    assert slots["tvl"]["aave-v3"]["current_tvl_usd"] == 100.0


def test_summarize_assess_counterparty_risk_into_slots():
    import json

    payload = json.dumps(
        {
            "wallet_address": "0x5853ed4f26a3fcea565b3fbc698bb19cdf6deb85",
            "verdict": "acceptable",
            "total_net_worth_usd": 27654.14,
            "liquidation": {"status": "no_debt", "worst_health_factor": None},
            "approval_summary": {"hacked_spenders": 0, "unlimited": 2},
        }
    )
    slots = summarize_into_slots("assess_counterparty_risk", payload, {})
    assert "counterparty_risk" in slots
    addr = "0x5853ed4f26a3fcea565b3fbc698bb19cdf6deb85"
    assert addr in slots["counterparty_risk"]
    assert slots["counterparty_risk"][addr]["verdict"] == "acceptable"
    assert slots["counterparty_risk"][addr]["total_net_worth_usd"] == 27654.14
    assert slots["counterparty_risk"][addr]["unlimited_approvals"] == 2


def test_summarize_validate_opportunity_into_slots():
    import json

    payload = json.dumps(
        {
            "pool_id": "747c1d2a-c668-4682-b9f9-296708a3dd90",
            "project": "aave-v3",
            "symbol": "USDC",
            "verdict": "sustainable",
            "sustainability_reason": "Pool exhibits healthy liquidity, consistent yields, and stable track record.",
            "yield_summary": {"apy": 4.5},
            "liquidity_summary": {"tvl_usd": 50000000.0},
        }
    )
    slots = summarize_into_slots("validate_opportunity", payload, {})
    assert "opportunity_validations" in slots
    pid = "747c1d2a-c668-4682-b9f9-296708a3dd90"
    assert pid in slots["opportunity_validations"]
    assert slots["opportunity_validations"][pid]["verdict"] == "sustainable"
    assert slots["opportunity_validations"][pid]["project"] == "aave-v3"
    assert slots["opportunity_validations"][pid]["symbol"] == "USDC"
    assert slots["opportunity_validations"][pid]["tvl_usd"] == 50000000.0
    assert slots["opportunity_validations"][pid]["apy"] == 4.5
