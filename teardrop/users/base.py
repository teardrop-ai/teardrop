# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Foundational state for the user/org data layer.

Holds the shared asyncpg pool, schema initialisation, password hashing helpers,
and small utilities. Kept free of intra-package dependencies so the other
``teardrop.users`` submodules can build on it without import cycles.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re

import asyncpg

logger = logging.getLogger(__name__)

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
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_org_invites_org ON org_invites (org_id)")
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
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens (user_id)")
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


def _generate_org_slug(org_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-")[:40]
