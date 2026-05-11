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
    assert slots["tvl"]["aave"]["note"] == "ok"
    assert "chain_breakdown" not in slots["tvl"]["aave"]
    assert "historical_series" not in slots["tvl"]["aave"]
