# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""CAPTCHA verification helpers (Cloudflare Turnstile).

This module is intentionally lightweight and side-effect free so auth routes can
use it directly. When TURNSTILE_SECRET_KEY is unset (default), verification
becomes a no-op that returns True for backward compatibility in local/dev/test
environments.
"""

from __future__ import annotations

import logging

import httpx

from teardrop.config import get_settings

logger = logging.getLogger(__name__)


async def verify_turnstile(token: str | None, *, remote_ip: str | None = None) -> bool:
    """Verify a Turnstile token against Cloudflare siteverify.

    Returns:
      - True when verification succeeds.
      - True when TURNSTILE_SECRET_KEY is unset (feature disabled).
      - False on missing token, verification failure, or network/server errors.
    """
    settings = get_settings()
    if not settings.turnstile_secret_key:
        return True
    if not token:
        return False

    data = {
        "secret": settings.turnstile_secret_key,
        "response": token,
    }
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.turnstile_verify_url, data=data)
            resp.raise_for_status()
            payload = resp.json()
            return bool(payload.get("success"))
    except Exception as exc:
        logger.warning("Turnstile verification request failed: %s", exc)
        return False
