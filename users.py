# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""User and organisation data layer (async Postgres via asyncpg).

Provides:
- Org / User Pydantic models
- init_user_db()     — create tables on startup
- create_org()       — register a new organisation
- create_user()      — register a new user within an org
- get_user_by_email()— look up user for authentication
- verify_secret()    — constant-time password verification
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ─── Models ───────────────────────────────────────────────────────────────────


class Org(BaseModel):
    id: str
    name: str
    slug: str = ""
    created_at: datetime


class User(BaseModel):
    id: str
    email: str
    org_id: str
    hashed_secret: str
    salt: str
    role: str  # "admin" | "user"
    is_active: bool
    is_verified: bool = True
    created_at: datetime


# ─── Password hashing (PBKDF2-SHA256, stdlib) ────────────────────────────────

_HASH_ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-SHA256


def _hash_secret(secret: str, salt: bytes | None = None) -> tuple[str, str]:
    """Hash a plaintext secret. Returns (hex_hash, hex_salt)."""
    if salt is None:
        salt = os.urandom(32)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, _HASH_ITERATIONS)
    return dk.hex(), salt.hex()


def verify_secret(secret: str, hashed: str, salt_hex: str) -> bool:
    """Constant-time comparison of a plaintext secret against stored hash."""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, _HASH_ITERATIONS)
    return hmac.compare_digest(dk.hex(), hashed)


# ─── Database initialisation ─────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_user_db(pool: asyncpg.Pool) -> None:
    """Create users/orgs tables if they don't exist."""
    global _pool
    _pool = pool
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS orgs (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            email         TEXT NOT NULL UNIQUE,
            org_id        TEXT NOT NULL REFERENCES orgs(id),
            hashed_secret TEXT NOT NULL,
            salt          TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user',
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            is_verified   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            token      TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used       BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS org_invites (
            token      TEXT PRIMARY KEY,
            org_id     TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            email      TEXT,
            role       TEXT NOT NULL DEFAULT 'user',
            invited_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used       BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_org_invites_org ON org_invites (org_id)"
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token        TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            org_id       TEXT NOT NULL,
            auth_method  TEXT NOT NULL,
            extra_claims JSONB NOT NULL DEFAULT '{}',
            created_at   TIMESTAMPTZ NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            revoked      BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens (user_id)"
    )
    logger.info("User tables ready (Postgres)")


async def close_user_db() -> None:
    """Release the pool reference (pool is closed by the caller)."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("User DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("User DB not initialised — call init_user_db() first")
    return _pool


# ─── CRUD ─────────────────────────────────────────────────────────────────────


async def create_org(name: str) -> Org:
    """Create a new organisation."""
    pool = _get_pool()
    org = Org(
        id=str(uuid.uuid4()),
        name=name,
        created_at=datetime.now(timezone.utc),
    )
    await pool.execute(
        "INSERT INTO orgs (id, name, created_at) VALUES ($1, $2, $3)",
        org.id,
        org.name,
        org.created_at,
    )
    return org


async def create_user(
    email: str,
    secret: str,
    org_id: str,
    role: str = "user",
    is_verified: bool = True,
) -> User:
    """Create a new user within an org. Returns the User (without plaintext secret)."""
    pool = _get_pool()
    hashed, salt_hex = _hash_secret(secret)
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        org_id=org_id,
        hashed_secret=hashed,
        salt=salt_hex,
        role=role,
        is_active=True,
        is_verified=is_verified,
        created_at=datetime.now(timezone.utc),
    )
    await pool.execute(
        "INSERT INTO users"
        " (id, email, org_id, hashed_secret, salt, role, is_active, is_verified, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        user.id,
        user.email,
        user.org_id,
        user.hashed_secret,
        user.salt,
        user.role,
        True,
        is_verified,
        user.created_at,
    )
    return user


async def get_user_by_email(email: str) -> User | None:
    """Look up an active user by email. Returns None if not found or inactive."""
    email = email.strip().lower()
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, org_id, hashed_secret, salt, role, is_active, is_verified, created_at"
        " FROM users WHERE email = $1",
        email,
    )
    if row is None:
        return None
    user = User(
        id=row["id"],
        email=row["email"],
        org_id=row["org_id"],
        hashed_secret=row["hashed_secret"],
        salt=row["salt"],
        role=row["role"],
        is_active=row["is_active"],
        is_verified=row["is_verified"],
        created_at=row["created_at"],
    )
    if not user.is_active:
        return None
    return user


async def get_org_by_id(org_id: str) -> Org | None:
    """Look up an organisation by its ID. Returns None if not found."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, name, created_at FROM orgs WHERE id = $1",
        org_id,
    )
    if row is None:
        return None
    return Org(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
    )


async def get_org_by_name(name: str) -> Org | None:
    """Look up an organisation by name. Returns None if not found."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, name, created_at FROM orgs WHERE name = $1",
        name,
    )
    if row is None:
        return None
    return Org(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
    )


async def get_user_by_org_id(org_id: str) -> User | None:
    """Look up the first active user in an org. Returns None if none found."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, org_id, hashed_secret, salt, role, is_active, is_verified, created_at"
        " FROM users WHERE org_id = $1 AND is_active = TRUE LIMIT 1",
        org_id,
    )
    if row is None:
        return None
    return User(
        id=row["id"],
        email=row["email"],
        org_id=row["org_id"],
        hashed_secret=row["hashed_secret"],
        salt=row["salt"],
        role=row["role"],
        is_active=row["is_active"],
        is_verified=row["is_verified"],
        created_at=row["created_at"],
    )


# ─── Org client credentials (M2M) ────────────────────────────────────────────


class OrgClientCredential(BaseModel):
    client_id: str
    org_id: str
    hashed_secret: str
    salt: str
    created_at: datetime


async def create_client_credential(org_id: str) -> tuple["OrgClientCredential", str]:
    """Create a new M2M client credential for an org.

    Returns ``(OrgClientCredential, plaintext_secret)``.
    The plaintext secret is only available at creation time — store it safely.
    """
    pool = _get_pool()
    client_id = str(uuid.uuid4())
    plaintext_secret = secrets.token_urlsafe(32)
    hashed, salt_hex = _hash_secret(plaintext_secret)
    now = datetime.now(timezone.utc)
    await pool.execute(
        "INSERT INTO org_client_credentials"
        " (client_id, org_id, hashed_secret, salt, created_at)"
        " VALUES ($1, $2, $3, $4, $5)",
        client_id,
        org_id,
        hashed,
        salt_hex,
        now,
    )
    cred = OrgClientCredential(
        client_id=client_id,
        org_id=org_id,
        hashed_secret=hashed,
        salt=salt_hex,
        created_at=now,
    )
    return cred, plaintext_secret


async def get_client_credential_by_id(client_id: str) -> "OrgClientCredential | None":
    """Look up an org client credential by client_id. Returns None if not found."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT client_id, org_id, hashed_secret, salt, created_at"
        " FROM org_client_credentials WHERE client_id = $1",
        client_id,
    )
    if row is None:
        return None
    return OrgClientCredential(
        client_id=row["client_id"],
        org_id=row["org_id"],
        hashed_secret=row["hashed_secret"],
        salt=row["salt"],
        created_at=row["created_at"],
    )


async def list_org_client_credentials(org_id: str) -> list["OrgClientCredential"]:
    """Return all client credentials for an org, ordered by creation date."""
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT client_id, org_id, hashed_secret, salt, created_at"
        " FROM org_client_credentials WHERE org_id = $1"
        " ORDER BY created_at DESC",
        org_id,
    )
    return [
        OrgClientCredential(
            client_id=r["client_id"],
            org_id=r["org_id"],
            hashed_secret=r["hashed_secret"],
            salt=r["salt"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def delete_org_client_credentials(org_id: str) -> None:
    """Delete all client credentials for an org (used during secret rotation)."""
    pool = _get_pool()
    await pool.execute(
        "DELETE FROM org_client_credentials WHERE org_id = $1",
        org_id,
    )


# ─── Self-serve registration ──────────────────────────────────────────────────


async def register_org_and_user(
    org_name: str,
    email: str,
    secret: str,
) -> tuple[Org, User]:
    """Transactionally create an org + an unverified user for self-serve registration.

    Raises asyncpg.UniqueViolationError if the org name or email already exists.
    """
    pool = _get_pool()
    hashed, salt_hex = _hash_secret(secret)
    now = datetime.now(timezone.utc)
    org_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    slug = re.sub(r'[^a-z0-9]+', '-', org_name.lower()).strip('-')[:40]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO orgs (id, name, slug, created_at) VALUES ($1, $2, $3, $4)",
                org_id, org_name, slug, now,
            )
            await conn.execute(
                "INSERT INTO users"
                " (id, email, org_id, hashed_secret, salt, role,"
                " is_active, is_verified, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                user_id, email, org_id, hashed, salt_hex, "user", True, False, now,
            )
    org = Org(id=org_id, name=org_name, slug=slug, created_at=now)
    user = User(
        id=user_id,
        email=email,
        org_id=org_id,
        hashed_secret=hashed,
        salt=salt_hex,
        role="user",
        is_active=True,
        is_verified=False,
        created_at=now,
    )
    return org, user


# ─── Email verification tokens ────────────────────────────────────────────────

_VERIFICATION_TOKEN_TTL_SECONDS = 24 * 3600  # 24 hours


async def create_verification_token(user_id: str) -> str:
    """Create a single-use email verification token. Returns the token string."""
    pool = _get_pool()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=_VERIFICATION_TOKEN_TTL_SECONDS)
    await pool.execute(
        "INSERT INTO email_verification_tokens (token, user_id, created_at, expires_at)"
        " VALUES ($1, $2, $3, $4)",
        token, user_id, now, expires_at,
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
                "SELECT user_id, expires_at, used"
                " FROM email_verification_tokens WHERE token = $1 FOR UPDATE",
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


class OrgInvite(BaseModel):
    token: str
    org_id: str
    email: str | None
    role: str
    invited_by: str
    created_at: datetime
    expires_at: datetime
    used: bool


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
        token, org_id, email, role, invited_by, now, expires_at,
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
        "SELECT token, org_id, email, role, invited_by, created_at, expires_at, used"
        " FROM org_invites WHERE token = $1",
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


# ─── Refresh tokens ───────────────────────────────────────────────────────────


class RefreshTokenRecord(BaseModel):
    token: str
    user_id: str
    org_id: str
    auth_method: str
    extra_claims: dict
    created_at: datetime
    expires_at: datetime


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
        token, user_id, org_id, auth_method, _json.dumps(extra_claims), now, expires_at,
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
                "UPDATE refresh_tokens"
                " SET revoked = TRUE, successor_token = $1"
                " WHERE token = $2",
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
    result = await pool.execute(
        "DELETE FROM refresh_tokens WHERE revoked = TRUE AND expires_at < NOW()"
    )
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
