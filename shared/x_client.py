# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Outbound client for publishing broadcast posts to X (Twitter) via v2 API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
import urllib.parse

import httpx

from teardrop.config import get_settings

logger = logging.getLogger(__name__)

_X_TWEETS_URL = "https://api.x.com/2/tweets"


class XClientError(Exception):
    """Raised when an X API call fails. Never contains credential material."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


def _percent_encode(val: str) -> str:
    return urllib.parse.quote(str(val), safe="")


def build_oauth1_header(
    method: str,
    url: str,
    *,
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Generate an OAuth 1.0a HMAC-SHA1 Authorization header."""
    oauth_nonce = nonce or secrets.token_hex(16)
    oauth_timestamp = timestamp or str(int(time.time()))

    params: dict[str, str] = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": oauth_nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": oauth_timestamp,
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }

    normalized_params = "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted(params.items()))
    base_string = f"{method.upper()}&{_percent_encode(url)}&{_percent_encode(normalized_params)}"
    signing_key = f"{_percent_encode(api_secret)}&{_percent_encode(access_token_secret)}"

    signature = base64.b64encode(
        hmac.new(
            signing_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")

    header_params = dict(params)
    header_params["oauth_signature"] = signature

    header_parts = [f'{_percent_encode(k)}="{_percent_encode(v)}"' for k, v in sorted(header_params.items())]
    return f"OAuth {', '.join(header_parts)}"


async def post_tweet(text: str, *, timeout: float = 10.0) -> str:
    """Publish a post to X. Returns the published tweet ID."""
    settings = get_settings()
    if not settings.x_broadcast_configured:
        raise XClientError("X broadcast credentials not configured")

    auth_header = build_oauth1_header(
        "POST",
        _X_TWEETS_URL,
        api_key=settings.x_api_key,
        api_secret=settings.x_api_secret,
        access_token=settings.x_access_token,
        access_token_secret=settings.x_access_token_secret,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _X_TWEETS_URL,
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/json",
            },
            json={"text": text},
        )

    if resp.status_code == 201:
        data = resp.json().get("data", {})
        tweet_id = str(data.get("id", ""))
        if not tweet_id:
            raise XClientError("X API response missing tweet ID", status_code=201)
        return tweet_id

    logger.warning("X API post failed status=%d", resp.status_code)
    raise XClientError(f"X API post failed with status {resp.status_code}", status_code=resp.status_code)
