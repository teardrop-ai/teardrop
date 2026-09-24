# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""JWT authentication utilities (RS256, self-issued).

Provides:
- create_access_token()  — mint a new JWT
- decode_access_token()  — verify and decode a JWT
- require_auth           — FastAPI dependency that enforces a valid Bearer token
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from teardrop.config import get_settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)
_AUTH_CHALLENGE = 'Bearer realm="teardrop", bootstrap_uri="/token", bootstrap_grant="x402"'
_AUTH_REQUIRED_DETAIL = "Missing authorization header. Bootstrap with POST /token using grant_type=x402."


def create_access_token(subject: str, extra_claims: dict | None = None) -> str:
    """Create a signed RS256 JWT."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_private_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, audience: str | None = None) -> dict:
    """Decode and validate a JWT. Raises on any failure.

    With ``audience``, unscoped tokens are accepted and ``aud``-scoped tokens must name it.
    Without it, ``aud``-scoped tokens are rejected.
    """
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_public_key,
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
        options={"verify_aud": False} if audience else None,
    )
    claimed = payload.get("aud")
    if audience and claimed:
        claimed_list = [claimed] if isinstance(claimed, str) else claimed
        if not isinstance(claimed_list, list) or audience not in claimed_list:
            raise jwt.InvalidAudienceError("Invalid audience")
    return payload


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict:
    """FastAPI dependency — extracts and validates a Bearer JWT.

    Returns the decoded payload dict on success, raises 401 on failure.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_AUTH_REQUIRED_DETAIL,
            headers={"WWW-Authenticate": _AUTH_CHALLENGE},
        )
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": _AUTH_CHALLENGE},
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("Invalid JWT: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token. {_AUTH_REQUIRED_DETAIL}",
            headers={"WWW-Authenticate": _AUTH_CHALLENGE},
        )
    return payload
