# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import base64
import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.x_client import XClientError, _percent_encode, build_oauth1_header, post_tweet


def test_percent_encode_rfc5849():
    assert _percent_encode("abcXYZ123-._~") == "abcXYZ123-._~"
    assert _percent_encode("hello world") == "hello%20world"
    assert _percent_encode("https://api.x.com/2/tweets") == "https%3A%2F%2Fapi.x.com%2F2%2Ftweets"


def test_build_oauth1_header_deterministic():
    header = build_oauth1_header(
        "POST",
        "https://api.x.com/2/tweets",
        api_key="test_api_key",
        api_secret="test_api_secret",
        access_token="test_token",
        access_token_secret="test_token_secret",
        nonce="test_nonce_123",
        timestamp="1700000000",
    )
    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="test_api_key"' in header
    assert 'oauth_nonce="test_nonce_123"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header
    assert 'oauth_timestamp="1700000000"' in header
    assert 'oauth_token="test_token"' in header
    assert 'oauth_version="1.0"' in header

    # Independent calculation of signature
    params = [
        ("oauth_consumer_key", "test_api_key"),
        ("oauth_nonce", "test_nonce_123"),
        ("oauth_signature_method", "HMAC-SHA1"),
        ("oauth_timestamp", "1700000000"),
        ("oauth_token", "test_token"),
        ("oauth_version", "1.0"),
    ]
    norm = "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted(params))
    base = f"POST&{_percent_encode('https://api.x.com/2/tweets')}&{_percent_encode(norm)}"
    key = f"{_percent_encode('test_api_secret')}&{_percent_encode('test_token_secret')}"
    expected_sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()

    assert f'oauth_signature="{_percent_encode(expected_sig)}"' in header


async def test_post_tweet_unconfigured(monkeypatch, test_settings):
    test_settings.x_api_key = ""
    test_settings.x_api_secret = ""
    test_settings.x_access_token = ""
    test_settings.x_access_token_secret = ""
    monkeypatch.setattr("shared.x_client.get_settings", lambda: test_settings)

    with pytest.raises(XClientError) as exc_info:
        await post_tweet("hello from teardrop")
    assert "not configured" in str(exc_info.value)


async def test_post_tweet_success(monkeypatch, test_settings):
    test_settings.x_api_key = "k"
    test_settings.x_api_secret = "s"
    test_settings.x_access_token = "t"
    test_settings.x_access_token_secret = "ts"
    monkeypatch.setattr("shared.x_client.get_settings", lambda: test_settings)

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"data": {"id": "18928374650123"}}

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = mock_resp
    monkeypatch.setattr("shared.x_client.httpx.AsyncClient", MagicMock(return_value=client))

    tweet_id = await post_tweet("test announcement")
    assert tweet_id == "18928374650123"
    client.post.assert_awaited_once()
    call_kwargs = client.post.await_args.kwargs
    assert call_kwargs["json"] == {"text": "test announcement"}
    assert "OAuth " in call_kwargs["headers"]["Authorization"]


async def test_post_tweet_error_sanitization(monkeypatch, test_settings):
    test_settings.x_api_key = "secret_key_material"
    test_settings.x_api_secret = "secret_api_material"
    test_settings.x_access_token = "secret_token_material"
    test_settings.x_access_token_secret = "secret_token_secret_material"
    monkeypatch.setattr("shared.x_client.get_settings", lambda: test_settings)

    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.text = "Forbidden - invalid oauth_token secret_token_material"

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = mock_resp
    monkeypatch.setattr("shared.x_client.httpx.AsyncClient", MagicMock(return_value=client))

    with pytest.raises(XClientError) as exc_info:
        await post_tweet("forbidden post")

    err = exc_info.value
    assert err.status_code == 403
    assert "secret_" not in str(err)
    assert "Forbidden" not in str(err)
