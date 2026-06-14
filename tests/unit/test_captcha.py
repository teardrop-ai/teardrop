"""Unit tests for shared.captcha Turnstile verification helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import shared.captcha as captcha_utils


def _mock_settings(*, turnstile_secret_key: str = ""):
    s = MagicMock()
    s.turnstile_secret_key = turnstile_secret_key
    s.turnstile_verify_url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    return s


@pytest.mark.anyio
class TestVerifyTurnstile:
    async def test_noop_when_feature_disabled(self):
        with patch("shared.captcha.get_settings", return_value=_mock_settings(turnstile_secret_key="")):
            with patch("shared.captcha.httpx.AsyncClient") as mock_client:
                ok = await captcha_utils.verify_turnstile(token=None)

        assert ok is True
        mock_client.assert_not_called()

    async def test_rejects_missing_token_when_enabled(self):
        with patch("shared.captcha.get_settings", return_value=_mock_settings(turnstile_secret_key="secret")):
            ok = await captcha_utils.verify_turnstile(token=None)

        assert ok is False

    async def test_returns_true_on_successful_siteverify(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"success": True})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("shared.captcha.get_settings", return_value=_mock_settings(turnstile_secret_key="secret")),
            patch("shared.captcha.httpx.AsyncClient", return_value=mock_client),
        ):
            ok = await captcha_utils.verify_turnstile(token="cf-token", remote_ip="1.2.3.4")

        assert ok is True
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        assert call_args[1]["data"]["secret"] == "secret"
        assert call_args[1]["data"]["response"] == "cf-token"
        assert call_args[1]["data"]["remoteip"] == "1.2.3.4"

    async def test_returns_false_on_http_error(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("shared.captcha.get_settings", return_value=_mock_settings(turnstile_secret_key="secret")),
            patch("shared.captcha.httpx.AsyncClient", return_value=mock_client),
        ):
            ok = await captcha_utils.verify_turnstile(token="cf-token")

        assert ok is False
