# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Wallet and SIWE nonce data layer (async Postgres via asyncpg).

Provides:
- Wallet model and CRUD (linking Ethereum addresses to users/orgs)
- SIWE nonce lifecycle (create, consume, replay-protection)
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone

import asyncpg
from pydantic import BaseModel

from cache import get_redis
from config import get_settings

logger = logging.getLogger(__name__)

# ─── Models ───────────────────────────────────────────────────────────────────


class Wallet(BaseModel):
    id: str
    address: str  # EIP-55 checksummed
    chain_id: int
    user_id: str
    org_id: str
    is_primary: bool
    created_at: datetime


# ─── Database initialisation ─────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_wallets_db(pool: asyncpg.Pool) -> None:
    """Create wallets and siwe_nonces tables if they don't exist."""
    global _pool
    _pool = pool
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS wallets (
            id         TEXT PRIMARY KEY,
            address    TEXT NOT NULL,
            chain_id   INTEGER NOT NULL,
            user_id    TEXT NOT NULL REFERENCES users(id),
            org_id     TEXT NOT NULL REFERENCES orgs(id),
            is_primary BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE (address, chain_id)
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_wallets_user ON wallets (user_id)")
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS siwe_nonces (
            nonce      TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            used       BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    logger.info("Wallets + SIWE nonce tables ready (Postgres)")


async def close_wallets_db() -> None:
    """Release the pool reference (pool is closed by the caller)."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Wallets DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Wallets DB not initialised — call init_wallets_db() first")
    return _pool


# ─── Wallet CRUD ──────────────────────────────────────────────────────────────


async def create_wallet(
    address: str,
    chain_id: int,
    user_id: str,
    org_id: str,
    is_primary: bool = False,
) -> Wallet:
    """Create a wallet record. Address must be EIP-55 checksummed."""
    pool = _get_pool()
    wallet = Wallet(
        id=str(uuid.uuid4()),
        address=address,
        chain_id=chain_id,
        user_id=user_id,
        org_id=org_id,
        is_primary=is_primary,
        created_at=datetime.now(timezone.utc),
    )
    await pool.execute(
        "INSERT INTO wallets (id, address, chain_id, user_id, org_id, is_primary, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7)",
        wallet.id,
        wallet.address,
        wallet.chain_id,
        wallet.user_id,
        wallet.org_id,
        wallet.is_primary,
        wallet.created_at,
    )
    return wallet


async def get_wallet_by_address(address: str, chain_id: int = 1) -> Wallet | None:
    """Look up a wallet by EIP-55 address and chain ID."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, address, chain_id, user_id, org_id, is_primary, created_at"
        " FROM wallets WHERE address = $1 AND chain_id = $2",
        address,
        chain_id,
    )
    if row is None:
        return None
    return Wallet(
        id=row["id"],
        address=row["address"],
        chain_id=row["chain_id"],
        user_id=row["user_id"],
        org_id=row["org_id"],
        is_primary=row["is_primary"],
        created_at=row["created_at"],
    )


async def get_wallets_by_user(user_id: str) -> list[Wallet]:
    """List all wallets linked to a user."""
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT id, address, chain_id, user_id, org_id, is_primary, created_at"
        " FROM wallets WHERE user_id = $1 ORDER BY created_at",
        user_id,
    )
    return [
        Wallet(
            id=r["id"],
            address=r["address"],
            chain_id=r["chain_id"],
            user_id=r["user_id"],
            org_id=r["org_id"],
            is_primary=r["is_primary"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def delete_wallet(wallet_id: str, user_id: str) -> bool:
    """Delete a wallet by ID (only if owned by user_id). Returns True if deleted."""
    pool = _get_pool()
    result = await pool.execute(
        "DELETE FROM wallets WHERE id = $1 AND user_id = $2",
        wallet_id,
        user_id,
    )
    return result == "DELETE 1"


# ─── SIWE Nonce management ───────────────────────────────────────────────────


async def create_nonce() -> str:
    """Generate and persist a single-use SIWE nonce.

    Uses Redis if available, otherwise falls back to Postgres.
    """
    # EIP-4361 requires alphanumeric-only nonces [a-zA-Z0-9]{8,}
    nonce = secrets.token_hex(16)
    settings = get_settings()

    # ── Redis path (multi-container, no cleanup needed) ──────────────────
    if (redis := get_redis()) is not None:
        try:
            key = f"teardrop:nonce:{nonce}"
            await redis.set(key, "1", ex=settings.siwe_nonce_ttl_seconds, nx=True)
            return nonce
        except Exception as exc:
            logger.warning("Redis nonce creation failed; falling back to Postgres: %s", exc)

    # ── Postgres fallback ──────────────────────────────────────────────────
    pool = _get_pool()
    await pool.execute(
        "INSERT INTO siwe_nonces (nonce, created_at) VALUES ($1, $2)",
        nonce,
        datetime.now(timezone.utc),
    )
    return nonce


async def consume_nonce(nonce: str, ttl_seconds: int = 300) -> bool:
    """Consume a nonce. Returns True if valid (exists, unused, within TTL).

    Uses Redis if available (atomic GETDEL), otherwise falls back to Postgres.
    """
    # ── Redis path (atomic get+delete) ─────────────────────────────────────
    if (redis := get_redis()) is not None:
        try:
            key = f"teardrop:nonce:{nonce}"
            # GETDEL is atomic: get the value and delete it in one operation (Redis 6.2+)
            result = await redis.getdel(key)
            return result is not None
        except Exception as exc:
            logger.warning("Redis nonce consumption failed; falling back to Postgres: %s", exc)

    # ── Postgres fallback ──────────────────────────────────────────────────
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        UPDATE siwe_nonces
           SET used = TRUE
         WHERE nonce = $1
           AND used = FALSE
           AND created_at > NOW() - INTERVAL '1 second' * $2
        RETURNING nonce
        """,
        nonce,
        float(ttl_seconds),
    )
    return row is not None
