# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared FastAPI dependencies for router modules.

Extracted verbatim from ``teardrop.app``. ``require_auth`` is re-exported here
so routers have a single dependency import surface; ``require_admin`` and
``_require_org_id`` preserve their original signatures and error semantics.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from teardrop.auth import require_auth

__all__ = ["require_auth", "require_admin", "require_org_admin", "_require_org_id"]


async def require_admin(
    payload: dict = Depends(require_auth),
) -> dict:
    """FastAPI dependency — requires an authenticated user with role=admin.

    Platform-level privilege: ``role=admin`` grants access to every org's data
    through the ``/admin/*`` routes (cross-tenant by design). Admin users are
    provisioned out of band only — self-service registration and org invites
    can never grant ``admin`` — so there is no privilege-escalation path, but a
    compromised admin JWT exposes all tenant data. Treat admin tokens as
    highly sensitive credentials. For org-scoped mutations use
    ``require_org_admin`` instead.
    """
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return payload


async def require_org_admin(
    payload: dict = Depends(require_auth),
) -> dict:
    """FastAPI dependency — requires an admin user bound to an org.

    Used for org-scoped financial mutations (marketplace settlement wallet,
    payout withdrawals, M2M credential rotation) where ordinary members must
    not act. Reuses the existing ``role=admin`` JWT model — there is no
    separate org-owner role — but additionally requires an ``org_id`` claim so
    the action is unambiguously scoped to a single org.
    """
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    if not payload.get("org_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token.",
        )
    return payload


def _require_org_id(payload: dict, detail: str = "No org_id in token.") -> str:
    """Extract org_id from a JWT payload or raise HTTP 400.

    Used by all org-scoped endpoints.  Preserves the exact status code and
    detail string that was previously inlined at every call site.
    """
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    return org_id
