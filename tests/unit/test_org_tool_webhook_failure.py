"""Unit tests for org_tools webhook failure handling — audit + breaker + sentry."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import org_tools


@pytest.mark.asyncio
async def test_on_webhook_failure_records_audit_event_and_breaker_increment():
    """A non-tripping failure writes an audit event and increments the breaker."""
    with patch("org_tools._record_event", new_callable=AsyncMock) as audit_mock, \
         patch("tool_health.record_failure", new_callable=AsyncMock, return_value=False) as breaker_mock:
        await org_tools._on_webhook_failure(
            "tool-1", "org-1", "search", "abc123def456", "timeout",
        )

    audit_mock.assert_awaited_once()
    args, kwargs = audit_mock.call_args
    assert args[0] == "org-1"
    assert args[1] == "tool-1"
    assert args[2] == "search"
    assert args[3] == "failed"
    assert kwargs["actor_id"] == "agent"
    assert kwargs["detail"]["error_type"] == "timeout"
    assert kwargs["detail"]["host_hash"] == "abc123def456"

    breaker_mock.assert_awaited_once_with("tool-1")


@pytest.mark.asyncio
async def test_on_webhook_failure_tripped_calls_auto_deactivate():
    """When breaker trips, auto_deactivate_tool_for_health is invoked."""
    with patch("org_tools._record_event", new_callable=AsyncMock), \
         patch("tool_health.record_failure", new_callable=AsyncMock, return_value=True), \
         patch("org_tools.sentry_sdk") as sentry_mock, \
         patch("marketplace.auto_deactivate_tool_for_health", new_callable=AsyncMock) as deact_mock:
        await org_tools._on_webhook_failure(
            "tool-1", "org-1", "search", "abc123def456", "http_error",
            status_code=500,
        )

    deact_mock.assert_awaited_once_with("tool-1")
    sentry_mock.capture_message.assert_called_once()


@pytest.mark.asyncio
async def test_on_webhook_failure_includes_status_code_in_detail():
    with patch("org_tools._record_event", new_callable=AsyncMock) as audit_mock, \
         patch("tool_health.record_failure", new_callable=AsyncMock, return_value=False):
        await org_tools._on_webhook_failure(
            "tool-1", "org-1", "search", "abc123", "http_error", status_code=502,
        )

    detail = audit_mock.call_args.kwargs["detail"]
    assert detail["status"] == 502
    assert detail["error_type"] == "http_error"


def test_hash_webhook_host_returns_deterministic_12_hex_chars():
    h1 = org_tools._hash_webhook_host("https://api.example.com/hook")
    h2 = org_tools._hash_webhook_host("https://api.example.com/different/path")
    h3 = org_tools._hash_webhook_host("https://other.host.com/hook")

    # Same host → same hash regardless of path.
    assert h1 == h2
    # Different host → different hash.
    assert h1 != h3
    # Twelve lowercase hex chars.
    assert len(h1) == 12
    assert all(c in "0123456789abcdef" for c in h1)
