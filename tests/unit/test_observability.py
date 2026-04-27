# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for the Sentry observability scrubber.

These tests assert that ``observability._before_send`` strips the
Teardrop-specific sensitive headers and extras before any event would
reach Sentry. The scrubber is the only line of defense between secrets
and a third-party SaaS, so coverage here is mandatory.
"""

from __future__ import annotations

from observability import (
    _FILTERED,
    _SENSITIVE_EXTRA_KEYS,
    _SENSITIVE_HEADERS,
    _before_send,
    init_sentry,
)


def test_authorization_header_is_filtered():
    event = {"request": {"headers": {"Authorization": "Bearer secret-jwt"}}}
    out = _before_send(event, {})
    assert out is not None
    assert out["request"]["headers"]["Authorization"] == _FILTERED


def test_x_payment_header_is_filtered_case_insensitive():
    event = {
        "request": {
            "headers": {
                "x-payment": "eyJhbGciOi...",
                "X-PAYMENT-RESPONSE": "settled",
                "stripe-signature": "t=...,v1=...",
                "Cookie": "session=abc",
            }
        }
    }
    out = _before_send(event, {})
    assert out is not None
    headers = out["request"]["headers"]
    assert headers["x-payment"] == _FILTERED
    assert headers["X-PAYMENT-RESPONSE"] == _FILTERED
    assert headers["stripe-signature"] == _FILTERED
    assert headers["Cookie"] == _FILTERED


def test_non_sensitive_header_is_preserved():
    event = {"request": {"headers": {"User-Agent": "stripe/1.0", "Authorization": "x"}}}
    out = _before_send(event, {})
    assert out is not None
    assert out["request"]["headers"]["User-Agent"] == "stripe/1.0"
    assert out["request"]["headers"]["Authorization"] == _FILTERED


def test_request_body_is_dropped():
    event = {"request": {"data": {"payment_payload": "hex...", "amount": 100}}}
    out = _before_send(event, {})
    assert out is not None
    assert out["request"]["data"] == _FILTERED


def test_nested_extras_are_filtered():
    event = {
        "extra": {
            "context": {
                "payment_payload": "0xabc",
                "private_key": "0xdeadbeef",
                "org_id": "org_123",
                "nested": {"client_secret": "sk_test_xxx", "ok": True},
            }
        }
    }
    out = _before_send(event, {})
    assert out is not None
    ctx = out["extra"]["context"]
    assert ctx["payment_payload"] == _FILTERED
    assert ctx["private_key"] == _FILTERED
    assert ctx["org_id"] == "org_123"  # not sensitive
    assert ctx["nested"]["client_secret"] == _FILTERED
    assert ctx["nested"]["ok"] is True


def test_stack_frame_vars_are_filtered():
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "settle",
                                "vars": {
                                    "payment_payload": "0xabc",
                                    "amount_usdc": 1000,
                                    "api_key": "sk-...",
                                },
                            }
                        ]
                    }
                }
            ]
        }
    }
    out = _before_send(event, {})
    assert out is not None
    frame_vars = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars["payment_payload"] == _FILTERED
    assert frame_vars["api_key"] == _FILTERED
    assert frame_vars["amount_usdc"] == 1000


def test_empty_event_does_not_raise():
    out = _before_send({}, {})
    assert out == {}


def test_event_without_request_or_extras_is_safe():
    event = {"message": "something happened", "level": "error"}
    out = _before_send(event, {})
    assert out is not None
    assert out["message"] == "something happened"


def test_list_of_dicts_in_extras_is_recursed():
    event = {
        "extra": {
            "calls": [
                {"tool": "foo", "api_key": "secret-1"},
                {"tool": "bar", "refresh_token": "secret-2"},
            ]
        }
    }
    out = _before_send(event, {})
    assert out is not None
    assert out["extra"]["calls"][0]["api_key"] == _FILTERED
    assert out["extra"]["calls"][1]["refresh_token"] == _FILTERED


def test_init_sentry_returns_false_when_dsn_empty():
    class Settings:
        sentry_dsn = ""
        sentry_environment = ""
        app_env = "test"

    assert init_sentry(Settings()) is False


def test_init_sentry_returns_false_when_dsn_whitespace():
    class Settings:
        sentry_dsn = "   "
        sentry_environment = ""
        app_env = "test"

    assert init_sentry(Settings()) is False


def test_sensitive_constants_lowercased():
    """All denylist entries must be lower-case for the matcher to work."""
    for key in _SENSITIVE_HEADERS:
        assert key == key.lower(), f"header {key!r} must be lowercase"
    for key in _SENSITIVE_EXTRA_KEYS:
        assert key == key.lower(), f"extra {key!r} must be lowercase"
