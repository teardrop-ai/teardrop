"""API test fixtures — httpx.AsyncClient with mocked dependencies.

ASGITransport does NOT trigger FastAPI's lifespan (startup/shutdown),
so we don't need LifespanManager and can simply mock DB functions per-test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def api_client(test_settings):
    """AsyncClient for a regular (non-admin) authenticated user.

    require_auth is overridden to return a fixed test payload.
    DB functions must be mocked per-test with monkeypatch.
    """
    from app import app
    from auth import require_auth

    async def _mock_auth():
        return {
            "sub": "test-user-id",
            "email": "test@example.com",
            "role": "user",
            "org_id": "test-org-id",
        }

    app.dependency_overrides[require_auth] = _mock_auth
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.pop(require_auth, None)


@pytest.fixture
async def admin_api_client(test_settings):
    """AsyncClient for an admin user.

    Both require_auth and require_admin are overridden.
    """
    from app import app, require_admin
    from auth import require_auth

    admin_payload = {
        "sub": "admin-user-id",
        "email": "admin@example.com",
        "role": "admin",
        "org_id": "test-org-id",
    }

    async def _mock_auth():
        return admin_payload

    async def _mock_admin():
        return admin_payload

    app.dependency_overrides[require_auth] = _mock_auth
    app.dependency_overrides[require_admin] = _mock_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(require_admin, None)


@pytest.fixture
async def anon_client(test_settings):
    """AsyncClient with NO auth overrides — tests that expect 401."""
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
