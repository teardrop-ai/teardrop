"""API tests for per-admin rate limiting on financial mutation endpoints.

A compromised or leaked admin token can hammer state-mutating financial
endpoints (top-up, withdrawal processing, marketplace sweep, pricing). Each of
these endpoints now calls ``_enforce_rate_limit`` keyed on the acting admin's
user id. These tests assert the 429 path by forcing the underlying sliding
window check to deny.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def _deny_rate_limit(monkeypatch):
    async def _denied(_key, _limit):
        return False, 0, 9999999999

    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", _denied)


@pytest.fixture
def _allow_rate_limit(monkeypatch):
    async def _allowed(_key, _limit):
        return True, 99, 0

    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", _allowed)


@pytest.mark.anyio
async def test_admin_topup_rate_limited(admin_api_client, _deny_rate_limit, monkeypatch):
    monkeypatch.setattr("teardrop.routers.admin.billing.admin_topup_credit", AsyncMock(return_value=1))
    resp = await admin_api_client.post(
        "/admin/credits/topup",
        json={"org_id": "org-abc", "amount_usdc": 1_000_000},
    )
    assert resp.status_code == 429


@pytest.mark.anyio
async def test_admin_topup_allowed_when_under_limit(admin_api_client, _allow_rate_limit, monkeypatch):
    monkeypatch.setattr("teardrop.routers.admin.billing.admin_topup_credit", AsyncMock(return_value=1_500_000))
    resp = await admin_api_client.post(
        "/admin/credits/topup",
        json={"org_id": "org-abc", "amount_usdc": 1_000_000},
    )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_admin_sweep_rate_limited(admin_api_client, _deny_rate_limit):
    resp = await admin_api_client.post("/admin/marketplace/sweep")
    assert resp.status_code == 429


@pytest.mark.anyio
async def test_admin_process_withdrawal_rate_limited(admin_api_client, _deny_rate_limit):
    resp = await admin_api_client.post("/admin/marketplace/process-withdrawal/wd-1")
    assert resp.status_code == 429


@pytest.mark.anyio
async def test_admin_complete_withdrawal_rate_limited(admin_api_client, _deny_rate_limit):
    resp = await admin_api_client.post(
        "/admin/marketplace/complete-withdrawal/wd-1",
        json={"tx_hash": "0xabcdef0123456789"},
    )
    assert resp.status_code == 429


@pytest.mark.anyio
async def test_admin_pricing_override_rate_limited(admin_api_client, _deny_rate_limit):
    resp = await admin_api_client.post(
        "/admin/pricing/tools",
        json={"tool_name": "acme/weather", "cost_usdc": 1_000_000},
    )
    assert resp.status_code == 429
