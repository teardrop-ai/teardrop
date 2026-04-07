"""Unit tests for auth.py — JWT round-trips, expiry, and require_auth dependency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import config
from auth import create_access_token, decode_access_token, require_auth


def test_encode_decode_roundtrip(test_settings):
    token = create_access_token("user-123", extra_claims={"role": "user"})
    payload = decode_access_token(token)
    assert payload["sub"] == "user-123"
    assert payload["role"] == "user"
    assert payload["iss"] == test_settings.jwt_issuer


def test_expired_token_raises(test_settings, monkeypatch):
    # Mint a token that expired 1 minute ago by patching timedelta in auth.
    import auth

    original = auth.timedelta

    def fake_timedelta(**kwargs):
        # Clamp expire to -60s regardless of what's passed
        return timedelta(minutes=-1)

    monkeypatch.setattr(auth, "timedelta", fake_timedelta)
    token = create_access_token("user-123")
    monkeypatch.setattr(auth, "timedelta", original)

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_invalid_signature_raises(test_settings, tmp_path):
    # Sign with one key, verify with another.
    token = create_access_token("user-123")

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pub = (
        other_key.public_key().to_cryptography_key()
        if hasattr(other_key.public_key(), "to_cryptography_key")
        else other_key.public_key()
    )
    other_pub_pem = (
        other_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            token,
            other_pub_pem,
            algorithms=["RS256"],
            issuer=test_settings.jwt_issuer,
        )


def test_wrong_issuer_raises(test_settings):
    token = create_access_token("user-123")
    settings = config.get_settings()
    with pytest.raises(jwt.InvalidIssuerError):
        jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=["RS256"],
            issuer="wrong-issuer",
        )


@pytest.mark.anyio
async def test_require_auth_valid_token(test_settings, test_jwt_token):
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=test_jwt_token)
    payload = await require_auth(credentials=creds)
    assert payload["sub"] == "test-user-id"
    assert payload["role"] == "user"


@pytest.mark.anyio
async def test_require_auth_missing_header(test_settings):
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(credentials=None)
    assert exc_info.value.status_code == 401


@pytest.mark.anyio
async def test_require_auth_invalid_token(test_settings):
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not.a.token")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(credentials=creds)
    assert exc_info.value.status_code == 401
