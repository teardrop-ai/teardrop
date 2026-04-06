# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
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
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone

import asyncpg
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ─── Models ───────────────────────────────────────────────────────────────────


class Org(BaseModel):
    id: str
    name: str
    created_at: datetime


class User(BaseModel):
    id: str
    email: str
    org_id: str
    hashed_secret: str
    salt: str
    role: str  # "admin" | "user"
    is_active: bool
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
            created_at    TIMESTAMPTZ NOT NULL
        )
        """
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
        org.id, org.name, org.created_at,
    )
    return org


async def create_user(
    email: str,
    secret: str,
    org_id: str,
    role: str = "user",
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
        created_at=datetime.now(timezone.utc),
    )
    await pool.execute(
        "INSERT INTO users (id, email, org_id, hashed_secret, salt, role, is_active, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        user.id, user.email, user.org_id, user.hashed_secret,
        user.salt, user.role, True, user.created_at,
    )
    return user


async def get_user_by_email(email: str) -> User | None:
    """Look up an active user by email. Returns None if not found or inactive."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, org_id, hashed_secret, salt, role, is_active, created_at"
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
        created_at=row["created_at"],
    )
    if not user.is_active:
        return None
    return user


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
        "SELECT id, email, org_id, hashed_secret, salt, role, is_active, created_at"
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
        client_id, org_id, hashed, salt_hex, now,
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
