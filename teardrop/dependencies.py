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

__all__ = ["require_auth", "require_admin", "_require_org_id"]


async def require_admin(
    payload: dict = Depends(require_auth),
) -> dict:
    """FastAPI dependency — requires an authenticated user with role=admin."""
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
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
