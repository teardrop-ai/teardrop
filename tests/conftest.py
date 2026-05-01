"""Shared pytest fixtures for the Teardrop test suite."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import config
from auth import create_access_token


@pytest.fixture
def test_settings(tmp_path, monkeypatch):
    """Override settings with test values, generating a fresh RSA keypair.

    Clears the LRU cache before and after so each test starts from a known state.
    """
    # Generate an RSA-2048 keypair into a temp directory.
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (tmp_path / "private.pem").write_bytes(priv_pem)
    (tmp_path / "public.pem").write_bytes(pub_pem)

    # Patch env vars — pydantic-settings will pick these up on next Settings().
    monkeypatch.setenv("JWT_PRIVATE_KEY_PATH", str(tmp_path / "private.pem"))
    monkeypatch.setenv("JWT_PUBLIC_KEY_PATH", str(tmp_path / "public.pem"))
    monkeypatch.setenv("JWT_CLIENT_ID", "test-client")
    monkeypatch.setenv("JWT_CLIENT_SECRET", "test-secret-abc123")
    monkeypatch.setenv("JWT_ISSUER", "teardrop-test")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "")

    # Clear the LRU cache so the patched env is picked up.
    config.get_settings.cache_clear()
    settings = config.get_settings()

    yield settings

    # Teardown: clear again so subsequent tests re-initialise cleanly.
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def initialize_rpc_semaphore(test_settings):
    """Automatically initialize the RPC semaphore for all tests."""
    from tools.definitions._rpc_semaphore import init_rpc_semaphore
    init_rpc_semaphore(test_settings.agent_rpc_semaphore_limit)


@pytest.fixture
def test_jwt_token(test_settings) -> str:
    """A valid JWT for a regular test user."""
    return create_access_token(
        subject="test-user-id",
        extra_claims={
            "email": "test@example.com",
            "role": "user",
            "org_id": "test-org-id",
        },
    )


@pytest.fixture
def admin_jwt_token(test_settings) -> str:
    """A valid JWT for an admin user."""
    return create_access_token(
        subject="admin-user-id",
        extra_claims={
            "email": "admin@example.com",
            "role": "admin",
            "org_id": "test-org-id",
        },
    )


@pytest.fixture
def auth_header(test_jwt_token: str) -> dict[str, str]:
    """Authorization header dict for regular user requests."""
    return {"Authorization": f"Bearer {test_jwt_token}"}


@pytest.fixture
def admin_auth_header(admin_jwt_token: str) -> dict[str, str]:
    """Authorization header dict for admin requests."""
    return {"Authorization": f"Bearer {admin_jwt_token}"}
