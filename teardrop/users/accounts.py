# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""CRUD for organisations and users, plus self-serve registration."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from teardrop.users.base import _generate_org_slug, _get_pool, _hash_secret
from teardrop.users.models import Org, User


async def create_org(name: str, acquisition_source: str = "") -> Org:
    """Create a new organisation."""
    pool = _get_pool()
    slug = _generate_org_slug(name)
    org = Org(
        id=str(uuid.uuid4()),
        name=name,
        slug=slug,
        acquisition_source=acquisition_source,
        created_at=datetime.now(timezone.utc),
    )
    await pool.execute(
        "INSERT INTO orgs (id, name, slug, acquisition_source, created_at) VALUES ($1, $2, $3, $4, $5)",
        org.id,
        org.name,
        org.slug,
        org.acquisition_source,
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
        "SELECT id, email, org_id, hashed_secret, salt, role, is_active, is_verified, created_at FROM users WHERE email = $1",
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


async def get_org_id_for_user(user_id: str) -> str | None:
    """Return the organisation ID for a user, or ``None`` when not found."""
    pool = _get_pool()
    row = await pool.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
    return str(row["org_id"]) if row is not None else None


async def get_org_by_id(org_id: str) -> Org | None:
    """Look up an organisation by its ID. Returns None if not found."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, name, acquisition_source, created_at FROM orgs WHERE id = $1",
        org_id,
    )
    if row is None:
        return None
    return Org(
        id=row["id"],
        name=row["name"],
        acquisition_source=row["acquisition_source"],
        created_at=row["created_at"],
    )


async def get_org_by_name(name: str) -> Org | None:
    """Look up an organisation by name. Returns None if not found."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, name, acquisition_source, created_at FROM orgs WHERE name = $1",
        name,
    )
    if row is None:
        return None
    return Org(
        id=row["id"],
        name=row["name"],
        acquisition_source=row["acquisition_source"],
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


async def register_org_and_user(
    org_name: str,
    email: str,
    secret: str,
    acquisition_source: str = "",
) -> tuple[Org, User]:
    """Transactionally create an org + an unverified user for self-serve registration.

    Raises asyncpg.UniqueViolationError if the org name or email already exists.
    """
    pool = _get_pool()
    hashed, salt_hex = _hash_secret(secret)
    now = datetime.now(timezone.utc)
    org_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    slug = _generate_org_slug(org_name)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO orgs (id, name, slug, acquisition_source, created_at) VALUES ($1, $2, $3, $4, $5)",
                org_id,
                org_name,
                slug,
                acquisition_source,
                now,
            )
            await conn.execute(
                "INSERT INTO users"
                " (id, email, org_id, hashed_secret, salt, role,"
                " is_active, is_verified, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                user_id,
                email,
                org_id,
                hashed,
                salt_hex,
                "user",
                True,
                False,
                now,
            )
    org = Org(id=org_id, name=org_name, slug=slug, acquisition_source=acquisition_source, created_at=now)
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
