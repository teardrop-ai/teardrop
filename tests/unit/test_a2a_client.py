"""Unit tests for a2a_client.py — SSRF guard, agent card discovery, message sending."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from a2a_client import (
    A2AAgentCard,
    A2AMessage,
    A2ASendMessageResponse,
    A2ATask,
    A2ATaskStatus,
    _agent_card_cache,
    _is_ip_blocked,
    discover_agent_card,
    extract_result_text,
    send_message,
    validate_url,
)

pytestmark = pytest.mark.anyio


# ─── SSRF Guard ───────────────────────────────────────────────────────────────


class TestIsIpBlocked:
    def test_loopback_blocked(self):
        assert _is_ip_blocked("127.0.0.1") is True

    def test_private_10_blocked(self):
        assert _is_ip_blocked("10.0.0.1") is True

    def test_private_172_blocked(self):
        assert _is_ip_blocked("172.16.0.1") is True

    def test_private_192_blocked(self):
        assert _is_ip_blocked("192.168.1.1") is True

    def test_link_local_blocked(self):
        assert _is_ip_blocked("169.254.1.1") is True

    def test_ipv6_loopback_blocked(self):
        assert _is_ip_blocked("::1") is True

    def test_ipv6_ula_blocked(self):
        assert _is_ip_blocked("fc00::1") is True

    def test_public_ip_allowed(self):
        assert _is_ip_blocked("8.8.8.8") is False

    def test_public_ipv6_allowed(self):
        assert _is_ip_blocked("2001:4860:4860::8888") is False

    def test_invalid_ip_blocked(self):
        assert _is_ip_blocked("not-an-ip") is True


class TestValidateUrl:
    def test_valid_https_url(self):
        with patch("a2a_client.socket") as mock_socket:
            mock_socket.getaddrinfo.return_value = [
                (2, 1, 6, "", ("93.184.216.34", 0)),
            ]
            mock_socket.AF_UNSPEC = 0
            mock_socket.SOCK_STREAM = 1
            assert validate_url("https://agent.example.com") is None

    def test_ftp_scheme_blocked(self):
        assert validate_url("ftp://example.com") is not None

    def test_no_hostname(self):
        assert validate_url("https://") is not None

    def test_private_ip_in_url(self):
        result = validate_url("https://192.168.1.1/api")
        assert result is not None
        assert "Blocked" in result

    def test_localhost_blocked(self):
        result = validate_url("https://127.0.0.1")
        assert result is not None
        assert "Blocked" in result

    def test_dns_resolves_to_private(self):
        with patch("a2a_client.socket") as mock_socket:
            mock_socket.getaddrinfo.return_value = [
                (2, 1, 6, "", ("10.0.0.1", 0)),
            ]
            mock_socket.AF_UNSPEC = 0
            mock_socket.SOCK_STREAM = 1
            result = validate_url("https://malicious.example.com")
            assert result is not None
            assert "blocked" in result.lower()

    def test_dns_failure(self):
        import socket

        with patch("a2a_client.socket") as mock_socket:
            mock_socket.getaddrinfo.side_effect = socket.gaierror("Name or service not known")
            mock_socket.AF_UNSPEC = 0
            mock_socket.SOCK_STREAM = 1
            mock_socket.gaierror = socket.gaierror
            result = validate_url("https://nonexistent.invalid")
            assert result is not None
            assert "DNS" in result


# ─── Agent Card Discovery ────────────────────────────────────────────────────


class TestDiscoverAgentCard:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _agent_card_cache.clear()
        yield
        _agent_card_cache.clear()

    async def test_happy_path(self):
        card_data = {"name": "TestAgent", "description": "A test agent", "url": "https://test.example.com"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = card_data
        mock_resp.raise_for_status = MagicMock()

        with patch("a2a_client.validate_url", return_value=None), \
             patch("a2a_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            card = await discover_agent_card("https://test.example.com")
            assert card.name == "TestAgent"

    async def test_cache_hit(self):
        card = A2AAgentCard(name="Cached", description="cached agent")
        _agent_card_cache["https://cached.example.com"] = (card, __import__("time").monotonic())

        with patch("a2a_client.validate_url", return_value=None):
            result = await discover_agent_card("https://cached.example.com")
            assert result.name == "Cached"

    async def test_ssrf_blocked(self):
        with pytest.raises(ValueError, match="SSRF"):
            await discover_agent_card("https://192.168.1.1")

    async def test_http_error(self):
        with patch("a2a_client.validate_url", return_value=None), \
             patch("a2a_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=MagicMock(status_code=404)
            )
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await discover_agent_card("https://missing.example.com")


# ─── Send Message ─────────────────────────────────────────────────────────────


class TestSendMessage:
    async def test_happy_path_task_response(self):
        task_data = {
            "id": "task-123",
            "status": {
                "state": "completed",
                "message": {
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "Done!"}],
                },
            },
            "artifacts": [
                {
                    "artifactId": "a1",
                    "parts": [{"kind": "text", "text": "Result here"}],
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = task_data
        mock_resp.raise_for_status = MagicMock()

        with patch("a2a_client.validate_url", return_value=None), \
             patch("a2a_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            response = await send_message("https://agent.example.com", "Do something")
            assert response.task is not None
            assert response.task.id == "task-123"
            assert response.task.status.state == "completed"

    async def test_jsonrpc_envelope(self):
        task_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "id": "task-456",
                "status": {"state": "completed"},
                "artifacts": [],
            },
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = task_data
        mock_resp.raise_for_status = MagicMock()

        with patch("a2a_client.validate_url", return_value=None), \
             patch("a2a_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            response = await send_message("https://agent.example.com", "Task")
            assert response.task is not None
            assert response.task.id == "task-456"

    async def test_ssrf_blocked(self):
        with pytest.raises(ValueError, match="SSRF"):
            await send_message("https://10.0.0.1", "Task")


# ─── Extract Result Text ─────────────────────────────────────────────────────


class TestExtractResultText:
    def test_from_artifact(self):
        from a2a_client import A2AArtifact, A2APart

        response = A2ASendMessageResponse(
            task=A2ATask(
                id="t1",
                status=A2ATaskStatus(state="completed"),
                artifacts=[
                    A2AArtifact(
                        artifact_id="a1",
                        parts=[A2APart(kind="text", text="Artifact text")],
                    ),
                ],
            ),
        )
        assert extract_result_text(response) == "Artifact text"

    def test_from_status_message(self):
        from a2a_client import A2APart

        response = A2ASendMessageResponse(
            task=A2ATask(
                id="t1",
                status=A2ATaskStatus(
                    state="completed",
                    message=A2AMessage(
                        role="agent",
                        parts=[A2APart(kind="text", text="Status text")],
                    ),
                ),
                artifacts=[],
            ),
        )
        assert extract_result_text(response) == "Status text"

    def test_no_task(self):
        response = A2ASendMessageResponse(raw={"error": "something"})
        result = extract_result_text(response)
        assert "error" in result.lower() or "something" in result

    def test_empty_response(self):
        response = A2ASendMessageResponse()
        result = extract_result_text(response)
        assert "No response" in result
