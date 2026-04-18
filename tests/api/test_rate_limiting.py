"""API integration tests for per-org rate limiting on POST /agent/run.

Verifies the org-level 429 guard is independent from the per-user guard,
and that exhausting one org's bucket does not affect another org.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import app as app_module


def _make_settings(*, org_agent_rpm: int = 100) -> MagicMock:
    """Mock settings with rate limits high enough not to interfere unless specified."""
    s = MagicMock()
    s.billing_enabled = False
    s.rate_limit_requests_per_minute = 10_000
    s.rate_limit_agent_rpm = 10_000
    s.rate_limit_auth_rpm = 10_000
    s.rate_limit_org_agent_rpm = org_agent_rpm
    s.app_env = "test"
    return s


def _make_payload(user_id: str, org_id: str) -> dict:
    return {
        "sub": user_id,
        "org_id": org_id,
        "email": f"{user_id}@test.com",
        "role": "user",
        "auth_method": "email",
    }


@pytest.fixture(autouse=True)
def _clear_rate_counters():
    app_module._rate_counters.clear()
    yield
    app_module._rate_counters.clear()


# ─── Org-level 429 ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_org_rate_limit_returns_429_with_scope_header(test_settings, monkeypatch):
    """When the org bucket is full, /agent/run returns 429 with X-RateLimit-Scope: org."""
    from app import app
    from auth import require_auth

    org_id = "org-rl-exhausted"
    org_agent_rpm = 3

    mock_settings = _make_settings(org_agent_rpm=org_agent_rpm)
    monkeypatch.setattr("app.settings", mock_settings)

    # Pre-fill the org bucket to capacity using real timestamps.
    now = time.time()
    app_module._rate_counters[f"run:org:{org_id}"] = [now - i * 0.1 for i in range(org_agent_rpm)]

    async def _auth():
        return _make_payload("user-1", org_id)

    app.dependency_overrides[require_auth] = _auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/run", json={"message": "hi"})
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 429
    assert resp.headers.get("X-RateLimit-Scope") == "org"
    assert "Organization rate limit" in resp.json()["detail"]
    assert resp.headers.get("Retry-After") == "60"


@pytest.mark.anyio
async def test_org_rate_limit_headers_contain_limit_and_remaining(test_settings, monkeypatch):
    """429 response from org limit must include X-RateLimit-Limit and X-RateLimit-Remaining."""
    from app import app
    from auth import require_auth

    org_id = "org-rl-headers"
    org_agent_rpm = 2

    mock_settings = _make_settings(org_agent_rpm=org_agent_rpm)
    monkeypatch.setattr("app.settings", mock_settings)

    now = time.time()
    app_module._rate_counters[f"run:org:{org_id}"] = [now - 0.1, now - 0.2]

    app.dependency_overrides[require_auth] = lambda: _make_payload("user-h", org_id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/run", json={"message": "hi"})
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 429
    assert resp.headers.get("X-RateLimit-Limit") == str(org_agent_rpm)
    assert resp.headers.get("X-RateLimit-Remaining") == "0"


# ─── Isolation between orgs ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_exhausted_org_does_not_block_other_org(test_settings, monkeypatch):
    """Exhausting org-A's bucket must not produce a 429 for org-B users."""
    from app import app
    from auth import require_auth

    org_a = "org-a-noisy"
    org_b = "org-b-innocent"
    org_agent_rpm = 2

    mock_settings = _make_settings(org_agent_rpm=org_agent_rpm)
    monkeypatch.setattr("app.settings", mock_settings)

    # Exhaust org-A.
    now = time.time()
    app_module._rate_counters[f"run:org:{org_a}"] = [now - 0.1, now - 0.2]

    # org-B should NOT receive 429.  In test environments the SSE stream may
    # raise a BaseExceptionGroup (checkpointer not initialised) once it passes
    # all gate checks — that is fine; it means rate limiting did NOT block it.
    got_org_rate_limit = False
    app.dependency_overrides[require_auth] = lambda: _make_payload("user-b", org_b)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/run", json={"message": "hi"})
        if resp.status_code == 429 and resp.headers.get("X-RateLimit-Scope") == "org":
            got_org_rate_limit = True
    except BaseException:
        # SSE stream failed for a reason unrelated to rate limiting.
        pass
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert not got_org_rate_limit, (
        "org-B should not be rate-limited because of org-A's exhausted bucket"
    )


# ─── User limit fires before org limit ───────────────────────────────────────


@pytest.mark.anyio
async def test_user_limit_checked_before_org_limit(test_settings, monkeypatch):
    """If per-user bucket is exhausted, 429 must NOT carry X-RateLimit-Scope: org."""
    from app import app
    from auth import require_auth

    user_id = "user-fast"
    org_id = "org-has-capacity"
    user_rpm = 2

    mock_settings = _make_settings(org_agent_rpm=10_000)
    mock_settings.rate_limit_agent_rpm = user_rpm
    monkeypatch.setattr("app.settings", mock_settings)

    # Exhaust only the user bucket.
    now = time.time()
    app_module._rate_counters[f"run:{user_id}"] = [now - 0.1, now - 0.2]

    app.dependency_overrides[require_auth] = lambda: _make_payload(user_id, org_id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/run", json={"message": "hi"})
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 429
    # User-level 429 does NOT carry the org scope header.
    assert resp.headers.get("X-RateLimit-Scope") != "org"


# ─── No org_id edge case ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_empty_org_id_skips_org_rate_limit(test_settings, monkeypatch):
    """JWTs without an org_id must not create a rate-limit bucket under the empty key."""
    from app import app
    from auth import require_auth

    mock_settings = _make_settings(org_agent_rpm=1)
    monkeypatch.setattr("app.settings", mock_settings)

    # No org_id in JWT.
    app.dependency_overrides[require_auth] = lambda: {
        "sub": "no-org-user",
        "org_id": "",
        "email": "x@test.com",
        "role": "user",
        "auth_method": "client_credentials",
    }
    # In test environments the SSE stream may raise BaseExceptionGroup once all
    # gate checks pass — that is acceptable; rate limiting was NOT the blocker.
    got_org_rate_limit = False
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/run", json={"message": "hi"})
        if resp.status_code == 429 and resp.headers.get("X-RateLimit-Scope") == "org":
            got_org_rate_limit = True
    except BaseException:
        pass
    finally:
        app.dependency_overrides.pop(require_auth, None)

    # Must not be a 429 caused by the org rate limit.
    assert not got_org_rate_limit, "Empty org_id should skip the org rate-limit check"
    # The empty-string key must not appear in the counter dict.
    assert "run:org:" not in app_module._rate_counters
