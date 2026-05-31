# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Email verification tokens and org invites."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from teardrop.users.base import _get_pool
from teardrop.users.models import OrgInvite

# ─── Email verification tokens ────────────────────────────────────────────────

_VERIFICATION_TOKEN_TTL_SECONDS = 24 * 3600  # 24 hours


async def create_verification_token(user_id: str) -> str:
    """Create a single-use email verification token. Returns the token string."""
    pool = _get_pool()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=_VERIFICATION_TOKEN_TTL_SECONDS)
    await pool.execute(
        "INSERT INTO email_verification_tokens (token, user_id, created_at, expires_at) VALUES ($1, $2, $3, $4)",
        token,
        user_id,
        now,
        expires_at,
    )
    return token


async def consume_verification_token(token: str) -> str | None:
    """Atomically validate and consume a verification token.

    Returns user_id on success, None if the token is invalid, expired, or already used.
    """
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, expires_at, used FROM email_verification_tokens WHERE token = $1 FOR UPDATE",
                token,
            )
            if row is None or row["used"] or row["expires_at"] < now:
                return None
            await conn.execute(
                "UPDATE email_verification_tokens SET used = TRUE WHERE token = $1",
                token,
            )
            return row["user_id"]


async def mark_user_verified(user_id: str) -> None:
    """Mark a user's email address as verified."""
    pool = _get_pool()
    await pool.execute(
        "UPDATE users SET is_verified = TRUE WHERE id = $1",
        user_id,
    )


# ─── Org invites ──────────────────────────────────────────────────────────────

_INVITE_TOKEN_TTL_HOURS = 72  # 3 days


async def create_org_invite(
    org_id: str,
    invited_by: str,
    email: str | None = None,
    role: str = "user",
    ttl_hours: int = _INVITE_TOKEN_TTL_HOURS,
) -> OrgInvite:
    """Create an org invite token. Returns the full OrgInvite record."""
    pool = _get_pool()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours)
    await pool.execute(
        "INSERT INTO org_invites (token, org_id, email, role, invited_by, created_at, expires_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7)",
        token,
        org_id,
        email,
        role,
        invited_by,
        now,
        expires_at,
    )
    return OrgInvite(
        token=token,
        org_id=org_id,
        email=email,
        role=role,
        invited_by=invited_by,
        created_at=now,
        expires_at=expires_at,
        used=False,
    )


async def get_org_invite(token: str) -> OrgInvite | None:
    """Look up a valid (not used, not expired) invite. Returns None if invalid."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    row = await pool.fetchrow(
        "SELECT token, org_id, email, role, invited_by, created_at, expires_at, used FROM org_invites WHERE token = $1",
        token,
    )
    if row is None or row["used"] or row["expires_at"] < now:
        return None
    return OrgInvite(
        token=row["token"],
        org_id=row["org_id"],
        email=row["email"],
        role=row["role"],
        invited_by=row["invited_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        used=row["used"],
    )


async def consume_org_invite(token: str) -> bool:
    """Atomically mark an invite as used.

    Returns True on success, False if the token is already used, expired, or not found.
    Uses SELECT FOR UPDATE to prevent concurrent double-redemption.
    """
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT used, expires_at FROM org_invites WHERE token = $1 FOR UPDATE",
                token,
            )
            if row is None or row["used"] or row["expires_at"] < now:
                return False
            await conn.execute(
                "UPDATE org_invites SET used = TRUE WHERE token = $1",
                token,
            )
            return True
