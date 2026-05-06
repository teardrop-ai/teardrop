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
