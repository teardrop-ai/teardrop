# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Authentication, registration, refresh-token, invite, and org-credential routes."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from shared.captcha import verify_turnstile
from shared.email import send_invite_email, send_verification_email
from teardrop import rate_limit as _rate_limit
from teardrop.auth import create_access_token, require_auth
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_org_admin
from teardrop.rate_limit import _enforce_rate_limit
from teardrop.siwe import _handle_siwe_login
from teardrop.users import (
    User,
    consume_org_invite,
    consume_verification_token,
    create_client_credential,
    create_org_invite,
    create_refresh_token,
    create_user,
    create_verification_token,
    delete_org_client_credentials,
    get_client_credential_by_id,
    get_org_by_id,
    get_org_invite,
    get_refresh_token_successor,
    get_user_by_email,
    list_org_client_credentials,
    mark_user_verified,
    register_org_and_user,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_secret,
)
from teardrop.wallets import create_nonce

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


class TokenRequest(BaseModel):
    # Client-credentials flow (machine-to-machine) — backward compatible
    client_id: str | None = None
    client_secret: str | None = None
    # User-credentials flow (human users)
    email: str | None = None
    secret: str | None = None
    # SIWE flow (wallet users)
    siwe_message: str | None = None
    siwe_signature: str | None = None


async def _issue_email_token_pair(user: User) -> dict:
    """Canonical access + refresh token issuance for email-authenticated users.

    Single source of truth for ``refresh_tokens.extra_claims`` shape on the
    email path. Used by ``/token`` (email flow), ``/register``, and
    ``/register/invite``. SIWE and client-credentials flows are intentionally
    not routed through this helper because their claim shapes differ.
    """
    extra_claims = {
        "org_id": user.org_id,
        "email": user.email,
        "role": user.role,
        "auth_method": "email",
    }
    access_token = create_access_token(subject=user.id, extra_claims=extra_claims)
    refresh_token = await create_refresh_token(
        user_id=user.id,
        org_id=user.org_id,
        auth_method="email",
        extra_claims=extra_claims,
        expire_days=settings.refresh_token_expire_days,
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.jwt_access_token_expire_minutes * 60,
        "refresh_token": refresh_token,
    }


@router.post("/token", tags=["Auth"])
async def token(body: TokenRequest, request: Request) -> JSONResponse:
    """Tri-mode token endpoint.

    Accepts one of:
      1. email+secret (user credentials)
      2. client_id+client_secret (machine-to-machine)
      3. siwe_message+siwe_signature (Sign-In with Ethereum)
    Returns a signed RS256 JWT.
    """
    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(
        f"auth:{client_ip}",
        settings.rate_limit_auth_rpm,
        detail="Rate limit exceeded. Please slow down.",
    )

    # ── User-credentials flow ──────────────────────────────────────────────
    if body.email and body.secret:
        email_key = body.email.strip().lower()
        lockout_key = f"token:failed:{email_key}"

        locked, retry_after = await _rate_limit.check_auth_lockout(
            lockout_key,
            threshold=settings.auth_lockout_threshold,
            window_seconds=settings.auth_lockout_window_seconds,
        )
        if locked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )

        email_allowed, _, _ = await _rate_limit._check_rate_limit(f"token:email:{email_key}", 5)
        if not email_allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please slow down.",
                headers={"Retry-After": "60"},
            )
        user = await get_user_by_email(email_key)
        if user is None or not verify_secret(body.secret, user.hashed_secret, user.salt):
            await _rate_limit.record_auth_failure(lockout_key, settings.auth_lockout_window_seconds)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user credentials",
            )
        await _rate_limit.clear_auth_failures(lockout_key)
        if settings.require_email_verification and not user.is_verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Please verify your email before signing in.",
            )
        return JSONResponse(content=await _issue_email_token_pair(user))

    # ── Client-credentials flow ────────────────────────────────────────────────
    if body.client_id and body.client_secret:
        # Try DB-backed org credential first (org-scoped M2M)
        db_cred = await get_client_credential_by_id(body.client_id)
        if db_cred is not None:
            if not verify_secret(body.client_secret, db_cred.hashed_secret, db_cred.salt):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid client credentials",
                )
            org_id = db_cred.org_id
        else:
            # Fall back to config-based credential (backward compat — org_id is empty)
            if not settings.jwt_client_secret:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid client credentials",
                )
            if body.client_id != settings.jwt_client_id or not hmac.compare_digest(
                body.client_secret, settings.jwt_client_secret
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid client credentials",
                )
            org_id = ""
        access_token = create_access_token(
            subject=body.client_id,
            extra_claims={"auth_method": "client_credentials", "org_id": org_id},
        )
        return JSONResponse(
            content={
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.jwt_access_token_expire_minutes * 60,
            }
        )

    # ── SIWE flow (Sign-In with Ethereum) ──────────────────────────────────
    if body.siwe_message and body.siwe_signature:
        return JSONResponse(content=await _handle_siwe_login(body.siwe_message, body.siwe_signature))

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Provide email+secret, client_id+client_secret, or siwe_message+siwe_signature.",
    )


@router.get("/auth/me", tags=["Auth"])
async def auth_me(payload: dict = Depends(require_auth)) -> JSONResponse:
    """Return identity claims for the currently authenticated user.

    Decodes the Bearer JWT and echoes back the stable claims, augmented
    with org_name fetched from the database so the frontend can display
    the organisation name without a separate endpoint.
    """
    org_id: str = payload.get("org_id", "")
    body: dict = {
        "user_id": payload["sub"],
        "org_id": org_id,
        "role": payload.get("role", "user"),
        "auth_method": payload.get("auth_method", ""),
        "email": payload.get("email", ""),
    }
    # Augment with org_name from the database when an org_id is present.
    # Falls back to "" for config-based client_credentials (no org row).
    if org_id:
        _org = await get_org_by_id(org_id)
        body["org_name"] = _org.name if _org else ""
    # Include wallet-specific fields only for SIWE sessions.
    if payload.get("auth_method") == "siwe":
        body["address"] = payload.get("address", "")
        body["chain_id"] = payload.get("chain_id", 1)
    return JSONResponse(content=body)


@router.get("/auth/siwe/nonce", tags=["Auth"])
async def siwe_nonce(request: Request) -> JSONResponse:
    """Generate a single-use nonce for SIWE authentication."""
    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"auth:{client_ip}", settings.rate_limit_auth_rpm)
    nonce = await create_nonce()
    return JSONResponse(content={"nonce": nonce})


# ─── Self-serve registration & email verification ──────────────────────────────

# Compiled once at import time. Intentionally permissive — catches obvious non-emails
# without rejecting valid edge cases. Full RFC 5322 validation is handled by the mail server.
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


class RegisterRequest(BaseModel):
    org_name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8, max_length=128)
    captcha_token: str | None = Field(default=None, max_length=4096)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address format")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        return v


@router.post("/register", tags=["Auth"], status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request) -> JSONResponse:
    """Self-serve org and user registration. Returns a JWT immediately.

    A verification email is sent when RESEND_API_KEY is configured.
    Set REQUIRE_EMAIL_VERIFICATION=true to gate /token login until verified.
    """
    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"register:{client_ip}", settings.rate_limit_register_rpm)
    if not settings.allow_public_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is disabled. Please use an invite.",
        )
    if settings.turnstile_secret_key:
        captcha_ok = await verify_turnstile(body.captcha_token, remote_ip=client_ip)
        if not captcha_ok:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CAPTCHA verification failed.",
            )
    email_allowed, _, _ = await _rate_limit._check_rate_limit(f"register:email:{body.email}", 3)
    if not email_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={"Retry-After": "60"},
        )
    try:
        org, user = await register_org_and_user(
            org_name=body.org_name,
            email=body.email,
            secret=body.password,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email or organisation name already exists.",
        )
    verification_token = await create_verification_token(user.id)
    asyncio.create_task(send_verification_email(user.email, verification_token, settings.app_base_url))
    logger.info("register org=%s user=%s", org.id, user.id)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=await _issue_email_token_pair(user),
    )


@router.get("/auth/verify-email", tags=["Auth"])
async def verify_email(token: str = Query(...)) -> JSONResponse:
    """Verify an email address via a one-time token."""
    user_id = await consume_verification_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Verification token is invalid, expired, or already used.",
        )
    await mark_user_verified(user_id)
    return JSONResponse(content={"verified": True})


class ResendVerificationRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)


@router.post("/auth/resend-verification", tags=["Auth"])
async def resend_verification(body: ResendVerificationRequest, request: Request) -> JSONResponse:
    """Re-send a verification email. Always returns 200 to prevent email oracle attacks."""
    client_ip = request.client.host if request.client else "unknown"
    resend_limit = max(1, settings.rate_limit_auth_rpm // 4)
    await _enforce_rate_limit(f"resend:{client_ip}", resend_limit)
    user = await get_user_by_email(body.email)
    if user is not None and not user.is_verified:
        verification_token = await create_verification_token(user.id)
        asyncio.create_task(send_verification_email(user.email, verification_token, settings.app_base_url))
    return JSONResponse(content={"message": "If that email is registered, a verification link has been sent."})


# ─── Refresh tokens ────────────────────────────────────────────────────────────


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/auth/refresh", tags=["Auth"])
async def refresh_token_endpoint(body: RefreshRequest, request: Request) -> JSONResponse:
    """Exchange a refresh token for a new access token and rotated refresh token.

    Idempotent: if the client retried after a network timeout (the old token was
    already rotated but the 200 was never delivered), the successor token is
    replayed within refresh_token_idempotency_window_seconds.
    """
    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"auth:{client_ip}", settings.rate_limit_auth_rpm)

    # ── Happy path: atomic rotation ───────────────────────────────────────
    result = await rotate_refresh_token(
        body.refresh_token,
        expire_days=settings.refresh_token_expire_days,
    )
    if result is not None:
        record, new_refresh = result
        access_token = create_access_token(subject=record.user_id, extra_claims=record.extra_claims)
        return JSONResponse(
            content={
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.jwt_access_token_expire_minutes * 60,
                "refresh_token": new_refresh,
            }
        )

    # ── Idempotency replay: token was already rotated ─────────────────────
    # The old token may have been consumed in a prior request whose response
    # was lost (network timeout).  If a successor exists and was created within
    # the idempotency window, replay the same tokens so the client is not
    # locked out.
    successor = await get_refresh_token_successor(body.refresh_token)
    if successor is not None:
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        window = settings.refresh_token_idempotency_window_seconds
        age = (_dt.now(_tz.utc) - successor.created_at).total_seconds()
        if age <= window:
            access_token = create_access_token(subject=successor.user_id, extra_claims=successor.extra_claims)
            return JSONResponse(
                content={
                    "access_token": access_token,
                    "token_type": "bearer",
                    "expires_in": settings.jwt_access_token_expire_minutes * 60,
                    "refresh_token": successor.token,
                }
            )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid, expired, or revoked refresh token.",
    )


class LogoutRequest(BaseModel):
    refresh_token: str


@router.post("/auth/logout", tags=["Auth"])
async def logout(
    body: LogoutRequest,
    payload: dict = Depends(require_auth),
) -> Response:
    """Revoke a refresh token (logout)."""
    await revoke_refresh_token(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Org invites ───────────────────────────────────────────────────────────────

# Invite roles a member may grant. ``org_invites`` exist (migration 017) so org
# members can add users WITHOUT involving a platform admin, so the privileged
# ``admin`` role is deliberately not grantable here — that would be a privilege
# escalation path. ``member`` is accepted as a backward-compatible alias (the
# SDK documents it) and normalised to the canonical ``user`` role.
_ALLOWED_INVITE_ROLES = frozenset({"user", "member"})


class CreateInviteRequest(BaseModel):
    email: str | None = Field(default=None, max_length=320)
    role: str = "user"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in _ALLOWED_INVITE_ROLES:
            raise ValueError(f"role must be one of: {sorted(_ALLOWED_INVITE_ROLES)}")
        # Normalise the SDK-facing alias to the canonical stored role.
        return "user" if v == "member" else v


@router.post("/org/invite", tags=["Auth"], status_code=status.HTTP_201_CREATED)
async def create_invite(
    body: CreateInviteRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Create an org invite link. Any authenticated org member may invite."""
    org_id = payload.get("org_id", "")
    user_id = payload["sub"]
    invite = await create_org_invite(
        org_id=org_id,
        invited_by=user_id,
        email=body.email,
        role=body.role,
    )
    invite_url = f"{settings.app_base_url.rstrip('/')}/register/invite?token={invite.token}" if settings.app_base_url else None
    if body.email:
        asyncio.create_task(send_invite_email(body.email, invite.token, org_id, settings.app_base_url))
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "token": invite.token,
            "invite_url": invite_url,
            "expires_at": invite.expires_at.isoformat(),
        },
    )


class AcceptInviteRequest(BaseModel):
    token: str
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8, max_length=128)


@router.post("/register/invite", tags=["Auth"], status_code=status.HTTP_201_CREATED)
async def register_via_invite(body: AcceptInviteRequest, request: Request) -> JSONResponse:
    """Accept an org invite token and create a new user account."""
    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"auth:{client_ip}", settings.rate_limit_auth_rpm)
    invite = await get_org_invite(body.token)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Invite token is invalid, expired, or already used.",
        )
    # Enforce email match if the invite was issued for a specific address.
    if invite.email and invite.email.lower() != body.email.lower():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This invite was sent to a different email address.",
        )
    consumed = await consume_org_invite(body.token)
    if not consumed:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Invite token is invalid, expired, or already used.",
        )
    try:
        user = await create_user(
            email=body.email,
            secret=body.password,
            org_id=invite.org_id,
            role=invite.role,
            is_verified=True,  # accepting the invite is the trust signal
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        )
    logger.info("register_via_invite org=%s user=%s", user.org_id, user.id)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=await _issue_email_token_pair(user),
    )


# ─── Org credentials endpoints (member-accessible) ──────────────────────────


@router.get("/org/credentials", tags=["Credentials"])
async def get_org_credentials(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List all M2M client credentials for the authenticated org.

    Returns client_id and created_at only — secrets are never stored in plain
    text and cannot be retrieved after creation.
    """
    org_id = _require_org_id(payload, "No org_id in token — credentials require an org-scoped session.")
    credentials = await list_org_client_credentials(org_id)
    return JSONResponse(content=[{"client_id": c.client_id, "created_at": c.created_at.isoformat()} for c in credentials])


@router.post("/org/credentials/regenerate", tags=["Credentials"])
async def regenerate_org_credentials(
    payload: dict = Depends(require_org_admin),
) -> JSONResponse:
    """Rotate org M2M credentials: delete all existing and issue a new pair.

    Admin-only: this destroys every existing machine credential for the org and
    reveals the replacement secret, so it must not be available to ordinary
    members. The new client_secret is returned exactly once — store it
    immediately.
    """
    org_id = _require_org_id(payload, "No org_id in token — credentials require an org-scoped session.")
    await delete_org_client_credentials(org_id)
    cred, plaintext_secret = await create_client_credential(org_id)
    logger.info("org_credentials_rotated org=%s rotated_by=%s", org_id, payload["sub"])
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "client_id": cred.client_id,
            "client_secret": plaintext_secret,
            "created_at": cred.created_at.isoformat(),
        },
    )
