"""API tests for GET /usage/me and admin usage endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from usage import UsageSummary

_SUMMARY = UsageSummary(
    total_runs=5,
    total_tokens_in=500,
    total_tokens_out=200,
    total_tool_calls=10,
    total_duration_ms=3000,
)


@pytest.mark.anyio
async def test_usage_me(api_client, monkeypatch):
    monkeypatch.setattr("app.get_usage_by_user", AsyncMock(return_value=_SUMMARY))

    resp = await api_client.get("/usage/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_runs"] == 5
    assert body["total_tokens_in"] == 500


@pytest.mark.anyio
async def test_usage_me_requires_auth(anon_client):
    resp = await anon_client.get("/usage/me")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_admin_usage_user(admin_api_client, monkeypatch):
    monkeypatch.setattr("app.get_usage_by_user", AsyncMock(return_value=_SUMMARY))

    resp = await admin_api_client.get("/admin/usage/some-user-id")
    assert resp.status_code == 200
    assert resp.json()["total_runs"] == 5


@pytest.mark.anyio
async def test_admin_usage_user_requires_admin(api_client):
    resp = await api_client.get("/admin/usage/some-user-id")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_admin_usage_org(admin_api_client, monkeypatch):
    monkeypatch.setattr("app.get_usage_by_org", AsyncMock(return_value=_SUMMARY))

    resp = await admin_api_client.get("/admin/usage/org/some-org-id")
    assert resp.status_code == 200
    assert resp.json()["total_tokens_out"] == 200


@pytest.mark.anyio
async def test_admin_usage_org_requires_admin(api_client):
    resp = await api_client.get("/admin/usage/org/some-org-id")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_usage_me_with_date_range(api_client, monkeypatch):
    monkeypatch.setattr(
        "app.get_usage_by_user", AsyncMock(return_value=UsageSummary())
    )
    resp = await api_client.get(
        "/usage/me",
        params={"start": "2024-01-01T00:00:00", "end": "2024-12-31T23:59:59"},
    )
    assert resp.status_code == 200
