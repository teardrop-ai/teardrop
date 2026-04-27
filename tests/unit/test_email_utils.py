"""Unit tests for email_utils.py — mocked httpx to avoid real email delivery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import email_utils


def _mock_settings(has_key: bool = True):
    s = MagicMock()
    s.resend_api_key = "re_test_key" if has_key else ""
    s.resend_from_email = "noreply@teardrop.ai"
    return s


# ─── send_verification_email ──────────────────────────────────────────────────


@pytest.mark.anyio
class TestSendVerificationEmail:
    async def test_sends_when_api_key_present(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("email_utils.get_settings", return_value=_mock_settings(has_key=True)),
            patch("email_utils.httpx.AsyncClient", return_value=mock_client),
        ):
            await email_utils.send_verification_email("user@test.com", "tok-123", "https://app.test")

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        # Verify the URL and Authorization header
        assert call_kwargs[0][0] == email_utils._RESEND_EMAILS_URL
        assert "Bearer re_test_key" in call_kwargs[1]["headers"]["Authorization"]
        payload = call_kwargs[1]["json"]
        assert payload["to"] == ["user@test.com"]
        assert "?token=tok-123" in payload["html"]

    async def test_skips_when_no_api_key(self):
        with patch("email_utils.get_settings", return_value=_mock_settings(has_key=False)):
            with patch("email_utils.httpx.AsyncClient") as mock_cls:
                await email_utils.send_verification_email("user@test.com", "tok", "https://app.test")

        mock_cls.assert_not_called()

    async def test_swallows_http_errors(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("email_utils.get_settings", return_value=_mock_settings(has_key=True)),
            patch("email_utils.httpx.AsyncClient", return_value=mock_client),
        ):
            # Must not raise
            await email_utils.send_verification_email("u@test.com", "tok", "https://app")

    async def test_uses_relative_url_when_no_base(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("email_utils.get_settings", return_value=_mock_settings(has_key=True)),
            patch("email_utils.httpx.AsyncClient", return_value=mock_client),
        ):
            await email_utils.send_verification_email("u@test.com", "t", "")

        payload = mock_client.post.call_args[1]["json"]
        assert "?token=t" in payload["html"]


# ─── send_invite_email ────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestSendInviteEmail:
    async def test_sends_when_api_key_present(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("email_utils.get_settings", return_value=_mock_settings(has_key=True)),
            patch("email_utils.httpx.AsyncClient", return_value=mock_client),
        ):
            await email_utils.send_invite_email("invite@test.com", "inv-tok", "org-1", "https://app.test")

        mock_client.post.assert_called_once()
        payload = mock_client.post.call_args[1]["json"]
        assert payload["to"] == ["invite@test.com"]
        assert "inv-tok" in payload["html"]

    async def test_skips_when_no_api_key(self):
        with patch("email_utils.get_settings", return_value=_mock_settings(has_key=False)):
            with patch("email_utils.httpx.AsyncClient") as mock_cls:
                await email_utils.send_invite_email("u@test.com", "tok", "org", "https://app")

        mock_cls.assert_not_called()

    async def test_swallows_http_errors(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("email_utils.get_settings", return_value=_mock_settings(has_key=True)),
            patch("email_utils.httpx.AsyncClient", return_value=mock_client),
        ):
            await email_utils.send_invite_email("u@test.com", "tok", "org", "https://app")

    async def test_uses_relative_url_when_no_base(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("email_utils.get_settings", return_value=_mock_settings(has_key=True)),
            patch("email_utils.httpx.AsyncClient", return_value=mock_client),
        ):
            await email_utils.send_invite_email("u@test.com", "t", "org-1", "")

        payload = mock_client.post.call_args[1]["json"]
        assert "?token=t" in payload["html"]
