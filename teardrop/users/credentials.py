# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org client credentials (M2M auth)."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from teardrop.users.base import _get_pool, _hash_secret
from teardrop.users.models import OrgClientCredential


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
        "INSERT INTO org_client_credentials (client_id, org_id, hashed_secret, salt, created_at) VALUES ($1, $2, $3, $4, $5)",
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
        "SELECT client_id, org_id, hashed_secret, salt, created_at FROM org_client_credentials WHERE client_id = $1",
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
