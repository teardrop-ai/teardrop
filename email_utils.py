# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Transactional email delivery via the Resend REST API.

Both functions are safe to use with asyncio.create_task() — they catch and log
all exceptions instead of raising, so a delivery failure never blocks the caller.

When RESEND_API_KEY is empty (default), calls are silent no-ops. This keeps
development and test environments clean without requiring mock SMTP servers.
"""

from __future__ import annotations

import logging

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

_RESEND_EMAILS_URL = "https://api.resend.com/emails"


async def send_verification_email(to_email: str, token: str, base_url: str) -> None:
    """Send an email verification link to a newly registered user."""
    settings = get_settings()
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not set — skipping verification email to %s", to_email)
        return

    base = base_url.rstrip("/") if base_url else ""
    verify_url = f"{base}/auth/verify-email?token={token}" if base else f"?token={token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _RESEND_EMAILS_URL,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.resend_from_email,
                    "to": [to_email],
                    "subject": "Verify your Teardrop email",
                    "html": (
                        "<p>Welcome to Teardrop!</p>"
                        "<p>Click the link below to verify your email address:</p>"
                        f"<p><a href='{verify_url}'>{verify_url}</a></p>"
                        "<p>This link expires in 24 hours.</p>"
                    ),
                },
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to send verification email to %s: %s", to_email, exc)


async def send_invite_email(to_email: str, token: str, org_id: str, base_url: str) -> None:
    """Send an org invite email."""
    settings = get_settings()
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not set — skipping invite email to %s", to_email)
        return

    base = base_url.rstrip("/") if base_url else ""
    invite_url = f"{base}/register/invite?token={token}" if base else f"?token={token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _RESEND_EMAILS_URL,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.resend_from_email,
                    "to": [to_email],
                    "subject": "You've been invited to join Teardrop",
                    "html": (
                        "<p>You've been invited to join an organisation on Teardrop.</p>"
                        "<p>Click the link below to accept the invitation:</p>"
                        f"<p><a href='{invite_url}'>{invite_url}</a></p>"
                        "<p>This invite expires in 72 hours.</p>"
                    ),
                },
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to send invite email to %s: %s", to_email, exc)
