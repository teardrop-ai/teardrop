# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Wallet and SIWE nonce data layer (async Postgres).

Provides:
- Wallet model and CRUD (linking Ethereum addresses to users/orgs)
- SIWE nonce lifecycle (create, consume, replay-protection)
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from shared.db_pool import PgPool
from teardrop.cache import get_redis
from teardrop.config import get_settings
from teardrop.users.base import _generate_org_slug, _hash_secret
from teardrop.users.models import Org, User

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


class WalletProvisioningResult(BaseModel):
    """Outcome of ``provision_org_for_wallet``.

    ``created`` is False when the (address, chain_id) pair was already
    provisioned — either by a concurrent request that won the wallets UNIQUE
    race or by a prior login. Callers must treat the returned org/user as
    authoritative in both cases.
    """

    org: Org
    user: User
    wallet: Wallet
    created: bool


# ─── Database initialisation ─────────────────────────────────────────────────

_pool: PgPool | None = None


async def init_wallets_db(pool: PgPool) -> None:
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
            used       BOOLEAN NOT NULL DEFAULT FALSE,
            address    TEXT
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


def _get_pool() -> PgPool:
    if _pool is None:
        raise RuntimeError("Wallets DB not initialised — call init_wallets_db() first")
    return _pool


def _wallet_from_row(row) -> Wallet:
    return Wallet(
        id=row["id"],
        address=row["address"],
        chain_id=row["chain_id"],
        user_id=row["user_id"],
        org_id=row["org_id"],
        is_primary=row["is_primary"],
        created_at=row["created_at"],
    )


async def _fetch_wallet_by_address(conn, address: str, chain_id: int | None = None) -> Wallet | None:
    if chain_id is None:
        row = await conn.fetchrow(
            "SELECT id, address, chain_id, user_id, org_id, is_primary, created_at"
            " FROM wallets WHERE LOWER(address) = LOWER($1) ORDER BY created_at LIMIT 1",
            address,
        )
    else:
        row = await conn.fetchrow(
            "SELECT id, address, chain_id, user_id, org_id, is_primary, created_at"
            " FROM wallets WHERE LOWER(address) = LOWER($1) AND chain_id = $2",
            address,
            chain_id,
        )
    return _wallet_from_row(row) if row is not None else None


async def _load_wallet_owner(conn, wallet: Wallet) -> WalletProvisioningResult:
    org_row = await conn.fetchrow(
        "SELECT id, name, slug, acquisition_source, created_at FROM orgs WHERE id = $1",
        wallet.org_id,
    )
    user_row = await conn.fetchrow(
        "SELECT id, email, org_id, hashed_secret, salt, role, is_active, is_verified, created_at FROM users WHERE id = $1",
        wallet.user_id,
    )
    if org_row is None or user_row is None:
        raise RuntimeError("Wallet owner records are missing")
    return WalletProvisioningResult(
        org=Org(
            id=org_row["id"],
            name=org_row["name"],
            slug=org_row["slug"],
            acquisition_source=org_row["acquisition_source"],
            created_at=org_row["created_at"],
        ),
        user=User(
            id=user_row["id"],
            email=user_row["email"],
            org_id=user_row["org_id"],
            hashed_secret=user_row["hashed_secret"],
            salt=user_row["salt"],
            role=user_row["role"],
            is_active=user_row["is_active"],
            is_verified=user_row["is_verified"],
            created_at=user_row["created_at"],
        ),
        wallet=wallet,
        created=False,
    )


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
    return await _fetch_wallet_by_address(pool, address, chain_id)


async def get_wallet_by_address_any_chain(address: str) -> Wallet | None:
    """Look up the first wallet for an EIP-55 address across all chains."""
    pool = _get_pool()
    return await _fetch_wallet_by_address(pool, address)


async def get_provisioning_state_by_payment_ref(payment_ref: str) -> dict | None:
    """Return the immutable provisioning event and latest settlement outcome."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT id, org_id, method, payer_address, chain_id, settlement_tx,
               payment_ref, amount_usdc, event_type, settlement_status, settlement_error, created_at
        FROM org_provisioning_events
        WHERE payment_ref = $1
        ORDER BY created_at ASC, id ASC
        """,
        payment_ref,
    )
    if not rows:
        return None
    provisioned = next((row for row in rows if row["event_type"] == "provisioned"), None)
    settlements = [row for row in rows if row["event_type"] == "settlement"]
    if provisioned is None:
        return None
    return {
        "provisioned": dict(provisioned),
        "latest_settlement": dict(settlements[-1]) if settlements else None,
    }


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


async def consume_nonce(nonce: str, ttl_seconds: int = 300, *, expected_address: str | None = None) -> bool:
    """Consume a nonce. Returns True if valid (exists, unused, within TTL).

    If *expected_address* is provided, the nonce must either have no bound
    address (pre-030 migration backward compat) or match the given address
    (case-insensitive).  This prevents cross-address nonce theft.

    Uses Redis if available (atomic GETDEL), otherwise falls back to Postgres.
    """
    norm_addr = expected_address.lower() if expected_address else None

    # ── Redis path (atomic get+delete) ─────────────────────────────────────
    if (redis := get_redis()) is not None:
        try:
            key = f"teardrop:nonce:{nonce}"
            # GETDEL is atomic: get the value and delete it in one operation (Redis 6.2+)
            result = await redis.getdel(key)
            if result is None:
                return False
            # Address binding check: value is "1" (legacy) or a stored address
            if norm_addr and result != "1" and result.lower() != norm_addr:
                logger.warning("Nonce address mismatch: stored=%s expected=%s", result, norm_addr)
                return False
            return True
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
           AND (address IS NULL OR LOWER(address) = LOWER($3) OR $3 IS NULL)
        RETURNING nonce
        """,
        nonce,
        float(ttl_seconds),
        expected_address,
    )
    return row is not None


async def provision_org_for_wallet(
    address: str,
    chain_id: int,
    acquisition_source: str = "siwe",
    *,
    payment_ref: str | None = None,
    amount_usdc: int = 0,
) -> WalletProvisioningResult:
    """Transactionally provision (or idempotently resolve) an org for a wallet.

    Security contract:
      * The org name embeds the FULL EIP-55 address. Short prefixes are
        grindable; two distinct addresses must never map to one org.
      * Idempotency is decided by the ``wallets(address, chain_id)`` UNIQUE
        constraint inside the same transaction as org/user creation — never by
        a name lookup, which is the race/takeover vector this replaces.
      * A machine-provisioned org gets an ``org_credits`` row with a hard
        daily spend cap so credit-rail runs can never exceed it.
    """
    if acquisition_source not in {"siwe", "x402"}:
        raise ValueError("Unsupported wallet provisioning source")
    if chain_id <= 0:
        raise ValueError("chain_id must be positive")
    if amount_usdc < 0:
        raise ValueError("amount_usdc must not be negative")
    if acquisition_source == "x402" and not payment_ref:
        raise ValueError("x402 provisioning requires a payment reference")

    pool = _get_pool()
    settings = get_settings()
    address = address.strip()
    address_lower = address.lower()
    email = f"{address_lower}@wallet"
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                address_lower,
            )

            existing_wallet = await _fetch_wallet_by_address(conn, address, chain_id)
            if existing_wallet is not None:
                if acquisition_source == "x402" and payment_ref:
                    await conn.execute(
                        """
                        INSERT INTO org_provisioning_events
                            (id, org_id, method, payer_address, chain_id, payment_ref,
                             amount_usdc, event_type, settlement_status)
                        VALUES ($1, $2, 'x402', $3, $4, $5, $6, 'provisioned', 'pending')
                        ON CONFLICT DO NOTHING
                        """,
                        str(uuid.uuid4()),
                        existing_wallet.org_id,
                        address,
                        chain_id,
                        payment_ref,
                        amount_usdc,
                    )
                return await _load_wallet_owner(conn, existing_wallet)

            existing_any_chain = await _fetch_wallet_by_address(conn, address)
            if existing_any_chain is not None:
                org_row = await conn.fetchrow(
                    "SELECT id, name, slug, acquisition_source, created_at FROM orgs WHERE id = $1",
                    existing_any_chain.org_id,
                )
                user_row = await conn.fetchrow(
                    "SELECT id, email, org_id, hashed_secret, salt, role, is_active, is_verified, created_at"
                    " FROM users WHERE id = $1",
                    existing_any_chain.user_id,
                )
                if org_row is None or user_row is None:
                    raise RuntimeError("Wallet owner records are missing")
            else:
                org_name = f"wallet-{address_lower}"
                org_slug = _generate_org_slug(org_name)
                org_row = await conn.fetchrow(
                    "SELECT id, name, slug, acquisition_source, created_at FROM orgs WHERE name = $1",
                    org_name,
                )

                if org_row is None:
                    org_row = await conn.fetchrow(
                        "INSERT INTO orgs (id, name, slug, acquisition_source, created_at)"
                        " VALUES ($1, $2, $3, $4, $5)"
                        " ON CONFLICT DO NOTHING"
                        " RETURNING id, name, slug, acquisition_source, created_at",
                        str(uuid.uuid4()),
                        org_name,
                        org_slug,
                        acquisition_source,
                        now,
                    )

                if org_row is None:
                    org_row = await conn.fetchrow(
                        "SELECT id, name, slug, acquisition_source, created_at FROM orgs WHERE name = $1",
                        org_name,
                    )

                if org_row is None or org_row["acquisition_source"] not in {"siwe", "x402"}:
                    if org_row is not None:
                        org_row = None
                    for _ in range(3):
                        org_name = f"wallet-{uuid.uuid4().hex[:12]}-{address_lower}"
                        org_slug = _generate_org_slug(org_name)
                        org_row = await conn.fetchrow(
                            "INSERT INTO orgs (id, name, slug, acquisition_source, created_at)"
                            " VALUES ($1, $2, $3, $4, $5)"
                            " ON CONFLICT DO NOTHING"
                            " RETURNING id, name, slug, acquisition_source, created_at",
                            str(uuid.uuid4()),
                            org_name,
                            org_slug,
                            acquisition_source,
                            now,
                        )
                        if org_row is not None:
                            break

                if org_row is None:
                    raise RuntimeError("Unable to provision wallet organisation")

            org_id = org_row["id"]
            org_source = org_row["acquisition_source"]
            if existing_any_chain is None:
                user_row = await conn.fetchrow(
                    "SELECT id, email, org_id, hashed_secret, salt, role, is_active, is_verified, created_at"
                    " FROM users WHERE org_id = $1 AND is_active = TRUE LIMIT 1",
                    org_id,
                )

            if user_row is None:
                user_id = str(uuid.uuid4())
                secret = secrets.token_urlsafe(32)
                hashed, salt_hex = _hash_secret(secret)
                await conn.execute(
                    "INSERT INTO users"
                    " (id, email, org_id, hashed_secret, salt, role, is_active, is_verified, created_at)"
                    " VALUES ($1, $2, $3, $4, $5, 'user', TRUE, TRUE, $6)",
                    user_id,
                    email,
                    org_id,
                    hashed,
                    salt_hex,
                    now,
                )
                user_row = {
                    "id": user_id,
                    "email": email,
                    "org_id": org_id,
                    "hashed_secret": hashed,
                    "salt": salt_hex,
                    "role": "user",
                    "is_active": True,
                    "is_verified": True,
                    "created_at": now,
                }

            if org_source in {"siwe", "x402"}:
                await conn.execute(
                    """
                    INSERT INTO org_credits (org_id, balance_usdc, spending_limit_usdc, updated_at)
                    VALUES ($1, 0, $2, NOW())
                    ON CONFLICT (org_id) DO NOTHING
                    """,
                    org_id,
                    settings.machine_org_daily_spend_limit_usdc,
                )
            else:
                await conn.execute(
                    "INSERT INTO org_credits (org_id, balance_usdc, updated_at)"
                    " VALUES ($1, 0, NOW()) ON CONFLICT (org_id) DO NOTHING",
                    org_id,
                )

            wallet_id = str(uuid.uuid4())
            inserted = await conn.fetchrow(
                "INSERT INTO wallets (id, address, chain_id, user_id, org_id, is_primary, created_at)"
                " VALUES ($1, $2, $3, $4, $5, TRUE, $6)"
                " ON CONFLICT (address, chain_id) DO NOTHING"
                " RETURNING id",
                wallet_id,
                address,
                chain_id,
                user_row["id"],
                org_id,
                now,
            )
            if inserted is None:
                existing_wallet = await _fetch_wallet_by_address(conn, address, chain_id)
                if existing_wallet is None:
                    raise RuntimeError("Wallet provisioning race could not be resolved")
                return await _load_wallet_owner(conn, existing_wallet)

            await conn.execute(
                """
                INSERT INTO org_provisioning_events
                    (id, org_id, method, payer_address, chain_id, settlement_tx,
                     payment_ref, amount_usdc, event_type, settlement_status)
                VALUES ($1, $2, $3, $4, $5, '', $6, $7, 'provisioned',
                        CASE WHEN $3 = 'x402' THEN 'pending' ELSE 'not_applicable' END)
                """,
                str(uuid.uuid4()),
                org_id,
                acquisition_source,
                address,
                chain_id,
                payment_ref,
                amount_usdc,
            )

            wallet = Wallet(
                id=wallet_id,
                address=address,
                chain_id=chain_id,
                user_id=user_row["id"],
                org_id=org_id,
                is_primary=existing_any_chain is None,
                created_at=now,
            )
            return WalletProvisioningResult(
                org=Org(
                    id=org_row["id"],
                    name=org_row["name"],
                    slug=org_row["slug"],
                    acquisition_source=org_row["acquisition_source"],
                    created_at=org_row["created_at"],
                ),
                user=User(
                    id=user_row["id"],
                    email=user_row["email"],
                    org_id=user_row["org_id"],
                    hashed_secret=user_row["hashed_secret"],
                    salt=user_row["salt"],
                    role=user_row["role"],
                    is_active=user_row["is_active"],
                    is_verified=user_row["is_verified"],
                    created_at=user_row["created_at"],
                ),
                wallet=wallet,
                created=True,
            )
