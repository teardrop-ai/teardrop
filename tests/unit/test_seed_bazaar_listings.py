# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Guards for scripts/seed_bazaar_listings.py: argument coverage and stop-on-failure payment flow."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import scripts.seed_bazaar_listings as seed


def test_every_paid_tool_has_seed_arguments():
    covered = set(seed.TOOL_ARGUMENTS) | set(seed.DYNAMIC_ARGUMENTS)
    assert not set(seed.seedable_tools()) - covered, "add new paid tools to TOOL_ARGUMENTS or EXCLUDED_TOOLS"


def test_seed_arguments_only_name_seedable_tools():
    from tools import registry

    assert not (set(seed.TOOL_ARGUMENTS) | set(seed.DYNAMIC_ARGUMENTS)) - set(seed.seedable_tools())
    assert all(registry.get(name) is not None for name in seed.EXCLUDED_TOOLS)


@pytest.mark.parametrize("tool_name", sorted(seed.TOOL_ARGUMENTS))
def test_static_seed_arguments_validate_against_tool_schema(tool_name):
    assert seed.resolve_arguments(tool_name) == seed.TOOL_ARGUMENTS[tool_name]


def _response(status: int, body: dict) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, text=json.dumps(body), json=lambda: body)


def _challenge(tool: str, amount: int = 2_000) -> dict:
    return {
        "x402Version": 2,
        "resource": {"url": "https://api/tools/mcp", "serviceName": "Teardrop", "tags": ["mcp"]},
        "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": str(amount)}],
        "extensions": {"bazaar": {"info": {"input": {"type": "mcp", "toolName": tool}}}},
    }


class _FakeSession:
    def __init__(self, paid: dict[str, SimpleNamespace] | None = None, amount: int = 2_000):
        self.paid = paid or {}
        self.amount = amount
        self.paid_calls: list[str] = []

    def post(self, url, json, headers, timeout):  # noqa: A002, ANN001
        tool = json["params"]["name"]
        if "X-PAYMENT" not in headers:
            return _response(402, _challenge(tool, self.amount))
        self.paid_calls.append(tool)
        receipt = {"_meta": {"x402/payment-response": {"transaction": f"0x{tool}"}}, "isError": False}
        return self.paid.get(tool, _response(200, {"result": receipt}))


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(seed, "resolve_arguments", lambda name: {})
    monkeypatch.setattr(seed, "sign_payment", lambda body, key: "signed")


def _run(session, tools, **overrides):
    kwargs = {"execute": True, "private_key": "0xkey", "max_total_usdc": 1_000_000, "delay_seconds": 0}
    return seed.run("https://api", tools, session=session, **{**kwargs, **overrides})


def test_dry_run_quotes_without_paying(stubbed):
    session = _FakeSession()

    assert _run(session, ["a", "b"], execute=False) == 0
    assert session.paid_calls == []


def test_execute_settles_each_tool(stubbed, capsys):
    session = _FakeSession()

    assert _run(session, ["a", "b"]) == 0
    assert session.paid_calls == ["a", "b"]
    assert "tx 0xb" in capsys.readouterr().out


def test_execute_stops_at_first_unsettled_call(stubbed):
    failed = _response(200, {"result": {"isError": True, "content": [{"type": "text", "text": "bad"}]}})
    session = _FakeSession(paid={"a": failed})

    assert _run(session, ["a", "b", "c"]) == 1
    assert session.paid_calls == ["a"]


def test_execute_refuses_quotes_above_budget(stubbed):
    session = _FakeSession(amount=600_000)

    assert _run(session, ["a", "b"]) == 1
    assert session.paid_calls == []


def test_quote_error_blocks_payment(stubbed, monkeypatch):
    session = _FakeSession()
    original = session.post
    monkeypatch.setattr(
        session,
        "post",
        lambda url, json, headers, timeout: (
            _response(200, {}) if json["params"]["name"] == "b" else original(url, json, headers, timeout)
        ),
    )

    assert _run(session, ["a", "b"]) == 1
    assert session.paid_calls == []


def test_execute_requires_private_key(stubbed):
    session = _FakeSession()

    assert _run(session, ["a"], private_key=None) == 1
    assert session.paid_calls == []


def test_execute_refuses_incomplete_metadata_unless_allowed(stubbed, monkeypatch):
    monkeypatch.setattr(
        seed, "quote_tool", lambda s, b, name, a: seed.Quote(name, {}, 2_000, "eip155:8453", {}, ["no serviceName"])
    )
    session = _FakeSession()

    assert _run(session, ["a"]) == 1
    assert session.paid_calls == []
    assert _run(session, ["a"], allow_warnings=True) == 0
    assert session.paid_calls == ["a"]


def test_quote_warns_when_service_metadata_missing():
    body = _challenge("get_gas_price")
    body["resource"] = {"url": "https://api/tools/mcp"}
    session = SimpleNamespace(post=lambda *a, **k: _response(402, body))

    quote = seed.quote_tool(session, "https://api", "get_gas_price", {})

    assert not quote.error
    assert any("serviceName" in warning for warning in quote.warnings)
