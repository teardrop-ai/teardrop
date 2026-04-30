"""API tests for POST /tools/test-webhook — pre-publish webhook diagnostic probe."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

_BODY = {
    "webhook_url": "https://example.com/webhook",
    "webhook_method": "POST",
    "payload": {"city": "San Francisco"},
    "timeout_seconds": 10,
}


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """Clear in-process rate-limit state between tests in this module."""
    import app as app_module

    app_module._rate_counters.clear()
    yield
    app_module._rate_counters.clear()


def _mock_session(*, status: int, body: bytes, content_type: str = "application/json"):
    """Build an AsyncMock aiohttp.ClientSession that returns a configured response."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.read = AsyncMock(return_value=body)
    mock_resp.headers = {"Content-Type": content_type}

    mock_session = AsyncMock()
    mock_session.post = AsyncMock(return_value=mock_resp)
    mock_session.get = AsyncMock(return_value=mock_resp)
    mock_session.put = AsyncMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


# ─── Auth ─────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_unauthenticated(anon_client):
    resp = await anon_client.post("/tools/test-webhook", json=_BODY)
    assert resp.status_code == 401


# ─── Success paths ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_success_2xx_json(api_client, monkeypatch):
    session = _mock_session(status=200, body=b'{"result": "ok"}')
    monkeypatch.setattr("app.async_validate_url", AsyncMock(return_value=None), raising=False)
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["status_code"] == 200
    assert data["response_body"] == {"result": "ok"}
    assert data["error"] is None
    assert isinstance(data["latency_ms"], int)
    assert data["latency_ms"] >= 0


@pytest.mark.anyio
async def test_success_get_method(api_client):
    session = _mock_session(status=200, body=b'{"ok": true}')
    body = {**_BODY, "webhook_method": "GET"}
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=body)
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    session.get.assert_awaited_once()


@pytest.mark.anyio
async def test_response_body_non_dict_wrapped(api_client):
    """If webhook returns a JSON list/scalar, it is wrapped in {"value": ...}."""
    session = _mock_session(status=200, body=b"[1, 2, 3]")
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["response_body"] == {"value": [1, 2, 3]}


# ─── Failure paths ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_4xx_response(api_client):
    session = _mock_session(status=404, body=b'{"error": "not found"}')
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] == 404
    assert "404" in data["error"]


@pytest.mark.anyio
async def test_5xx_response(api_client):
    session = _mock_session(status=500, body=b'{"error": "server error"}')
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] == 500


@pytest.mark.anyio
async def test_non_json_content_type(api_client):
    session = _mock_session(status=200, body=b"<html>oops</html>", content_type="text/html")
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] == 200
    assert data["response_body"] is None
    assert "non-JSON" in data["error"]


@pytest.mark.anyio
async def test_invalid_json_body(api_client):
    session = _mock_session(status=200, body=b"not json {{{")
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    data = resp.json()
    assert data["success"] is False
    assert data["response_body"] is None
    assert "invalid" in data["error"].lower() or "truncated" in data["error"].lower()


@pytest.mark.anyio
async def test_timeout(api_client):
    mock_session = AsyncMock()
    mock_session.post = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] is None
    assert "timed out" in data["error"]


@pytest.mark.anyio
async def test_connection_error(api_client):
    mock_session = AsyncMock()
    mock_session.post = AsyncMock(side_effect=aiohttp.ClientConnectorError(MagicMock(), OSError("nope")))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] is None
    assert "Connection failed" in data["error"]


# ─── SSRF / validation ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ssrf_sync_blocks_localhost(api_client):
    """The synchronous _validate_webhook_url path rejects loopback before DNS."""
    body = {**_BODY, "webhook_url": "http://127.0.0.1/anything"}
    resp = await api_client.post("/tools/test-webhook", json=body)
    assert resp.status_code == 422
    assert "Unsafe webhook URL" in resp.json()["detail"]


@pytest.mark.anyio
async def test_ssrf_async_dns_block(api_client):
    """The async DNS re-check rejects when async_validate_url returns an error."""
    with patch(
        "tools.definitions.http_fetch.async_validate_url",
        new=AsyncMock(return_value="resolves to blocked IP"),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    assert resp.status_code == 422
    assert "Unsafe webhook URL" in resp.json()["detail"]


@pytest.mark.anyio
async def test_auth_header_inconsistent_rejected(api_client):
    body = {**_BODY, "auth_header_value": "Bearer xyz"}  # name missing
    with patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)):
        resp = await api_client.post("/tools/test-webhook", json=body)
    assert resp.status_code == 422
    assert "auth_header_name is required" in resp.json()["detail"]


# ─── Auth header forwarding ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_auth_header_forwarded_to_webhook(api_client):
    session = _mock_session(status=200, body=b'{"ok": true}')
    body = {
        **_BODY,
        "auth_header_name": "Authorization",
        "auth_header_value": "Bearer secret-xyz",
    }
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=body)

    assert resp.status_code == 200
    # Verify the header was passed through to the webhook call
    session.post.assert_awaited_once()
    _args, kwargs = session.post.call_args
    sent_headers = kwargs.get("headers", {})
    assert sent_headers.get("Authorization") == "Bearer secret-xyz"
    assert sent_headers.get("Content-Type") == "application/json"


@pytest.mark.anyio
async def test_auth_header_value_not_in_response(api_client):
    """The auth header value must never appear in the response body."""
    session = _mock_session(status=200, body=b'{"ok": true}')
    body = {
        **_BODY,
        "auth_header_name": "Authorization",
        "auth_header_value": "Bearer secret-xyz",
    }
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=body)
    assert "secret-xyz" not in resp.text


# ─── Rate limit ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_rate_limit_enforced(api_client, monkeypatch):
    """When _check_rate_limit returns allowed=False, endpoint returns 429."""

    async def _denied(_key, _limit):
        return False, 0, 9999999999

    monkeypatch.setattr("app._check_rate_limit", _denied)
    with patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    assert resp.status_code == 429
    assert "Rate limit exceeded" in resp.json()["detail"]


# ─── No side effects on audit / breaker ───────────────────────────────────────


@pytest.mark.anyio
async def test_no_audit_event_recorded(api_client, monkeypatch):
    """A test call must not write to org_tool_events."""
    record_event_mock = AsyncMock()
    monkeypatch.setattr("org_tools._record_event", record_event_mock)
    session = _mock_session(status=200, body=b'{"ok": true}')
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    assert resp.status_code == 200
    record_event_mock.assert_not_called()


@pytest.mark.anyio
async def test_no_circuit_breaker_interaction(api_client, monkeypatch):
    """A test call must not call tool_health.record_failure / record_success."""
    record_failure = AsyncMock()
    record_success = AsyncMock()
    monkeypatch.setattr("tool_health.record_failure", record_failure)
    monkeypatch.setattr("tool_health.record_success", record_success)
    session = _mock_session(status=500, body=b'{"err": "x"}')
    with (
        patch("aiohttp.ClientSession", return_value=session),
        patch("tools.definitions.http_fetch.async_validate_url", new=AsyncMock(return_value=None)),
    ):
        resp = await api_client.post("/tools/test-webhook", json=_BODY)
    assert resp.status_code == 200
    record_failure.assert_not_called()
    record_success.assert_not_called()


# ─── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_timeout_seconds_out_of_range(api_client):
    body = {**_BODY, "timeout_seconds": 60}  # max is 30
    resp = await api_client.post("/tools/test-webhook", json=body)
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_invalid_method(api_client):
    body = {**_BODY, "webhook_method": "DELETE"}
    resp = await api_client.post("/tools/test-webhook", json=body)
    assert resp.status_code == 422
