"""Unit tests for a2a_client.send_message() auth_header parameter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import a2a_client


@pytest.mark.asyncio
async def test_send_message_with_auth_header():
    """When auth_header is provided, Authorization header should be set."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "task-1", "status": {"state": "completed"}}
    mock_resp.raise_for_status = MagicMock()

    captured_headers = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured_headers.update(kwargs.get("headers", {}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return mock_resp

    with patch.object(a2a_client.httpx, "AsyncClient", FakeClient), \
         patch.object(a2a_client, "validate_url", return_value=None):
        await a2a_client.send_message(
            "https://agent.example.com",
            "hello",
            auth_header="my-jwt-token",
        )

    assert captured_headers.get("Authorization") == "Bearer my-jwt-token"


@pytest.mark.asyncio
async def test_send_message_without_auth_header():
    """When auth_header is None, no Authorization header should be set."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "task-1", "status": {"state": "completed"}}
    mock_resp.raise_for_status = MagicMock()

    captured_headers = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured_headers.update(kwargs.get("headers", {}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return mock_resp

    with patch.object(a2a_client.httpx, "AsyncClient", FakeClient), \
         patch.object(a2a_client, "validate_url", return_value=None):
        await a2a_client.send_message(
            "https://agent.example.com",
            "hello",
        )

    assert "Authorization" not in captured_headers
