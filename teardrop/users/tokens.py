# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Refresh token storage, rotation, and cleanup."""

from __future__ import annotations

import json as _json
import secrets
from datetime import datetime, timedelta, timezone

from teardrop.users.base import _get_pool
from teardrop.users.models import RefreshTokenRecord


async def create_refresh_token(
    user_id: str,
    org_id: str,
    auth_method: str,
    extra_claims: dict,
    expire_days: int,
) -> str:
    """Store a refresh token and return the token string."""
    pool = _get_pool()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=expire_days)
    await pool.execute(
        "INSERT INTO refresh_tokens"
        " (token, user_id, org_id, auth_method, extra_claims, created_at, expires_at)"
        " VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)",
        token,
        user_id,
        org_id,
        auth_method,
        _json.dumps(extra_claims),
        now,
        expires_at,
    )
    return token


async def rotate_refresh_token(
    old_token: str,
    expire_days: int,
) -> tuple[RefreshTokenRecord, str] | None:
    """Atomically rotate a refresh token in a single transaction.

    Validates the old token, inserts the new replacement token, and marks the
    old one as revoked — all within one DB transaction.  If the INSERT fails,
    the UPDATE never runs, so the old token remains valid and the caller can
    retry safely.

    Returns (record, new_token_string) on success, None if the old token is
    invalid, expired, or already revoked.
    """
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    new_token = secrets.token_urlsafe(32)
    new_expires_at = now + timedelta(days=expire_days)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT token, user_id, org_id, auth_method, extra_claims,"
                " created_at, expires_at, revoked"
                " FROM refresh_tokens WHERE token = $1 FOR UPDATE",
                old_token,
            )
            if row is None or row["revoked"] or row["expires_at"] < now:
                return None

            extra = row["extra_claims"]
            if isinstance(extra, str):
                extra = _json.loads(extra)
            elif extra is None:
                extra = {}

            # Insert the new token BEFORE revoking the old one.
            # If this INSERT fails, the transaction rolls back and the old token
            # stays valid — no lockout.
            await conn.execute(
                "INSERT INTO refresh_tokens"
                " (token, user_id, org_id, auth_method, extra_claims, created_at, expires_at)"
                " VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)",
                new_token,
                row["user_id"],
                row["org_id"],
                row["auth_method"],
                _json.dumps(extra),
                now,
                new_expires_at,
            )

            # Now safe to mark old token as consumed with a pointer to successor.
            await conn.execute(
                "UPDATE refresh_tokens SET revoked = TRUE, successor_token = $1 WHERE token = $2",
                new_token,
                old_token,
            )

            return (
                RefreshTokenRecord(
                    token=row["token"],
                    user_id=row["user_id"],
                    org_id=row["org_id"],
                    auth_method=row["auth_method"],
                    extra_claims=extra,
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                ),
                new_token,
            )


async def get_refresh_token_successor(old_token: str) -> RefreshTokenRecord | None:
    """Return the successor token record for an already-rotated refresh token.

    Used for idempotency replay: if a client retried because it never received
    the /auth/refresh response, we can replay the same new token rather than
    forcing a re-login.  Returns None if no successor exists (i.e. the token
    was revoked via logout, is expired, or was never issued).
    """
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT rt.token, rt.user_id, rt.org_id, rt.auth_method, rt.extra_claims,"
        " rt.created_at, rt.expires_at"
        " FROM refresh_tokens old"
        " JOIN refresh_tokens rt ON rt.token = old.successor_token"
        " WHERE old.token = $1"
        "  AND old.successor_token IS NOT NULL"
        "  AND rt.expires_at > NOW()",
        old_token,
    )
    if row is None:
        return None
    extra = row["extra_claims"]
    if isinstance(extra, str):
        extra = _json.loads(extra)
    elif extra is None:
        extra = {}
    return RefreshTokenRecord(
        token=row["token"],
        user_id=row["user_id"],
        org_id=row["org_id"],
        auth_method=row["auth_method"],
        extra_claims=extra,
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


async def cleanup_expired_refresh_tokens() -> int:
    """Delete revoked refresh tokens whose expiry has passed.

    Only touches rows that are both revoked AND past expires_at — active tokens
    are never deleted.  Returns the count of deleted rows.
    """
    pool = _get_pool()
    result = await pool.execute("DELETE FROM refresh_tokens WHERE revoked = TRUE AND expires_at < NOW()")
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def revoke_refresh_token(token: str) -> None:
    """Revoke a refresh token (logout). No-op if already revoked or not found."""
    pool = _get_pool()
    await pool.execute(
        "UPDATE refresh_tokens SET revoked = TRUE WHERE token = $1",
        token,
    )
