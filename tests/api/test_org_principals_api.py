# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.anyio
async def test_principal_limits_require_admin(api_client):
    response = await api_client.get("/org/principals/spend-limits")
    assert response.status_code == 403


@pytest.mark.anyio
async def test_upsert_principal_limit_is_org_scoped_and_repeatable(admin_api_client):
    from teardrop.main import app

    now = datetime.now(timezone.utc)
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "principal_id": "agent-1",
            "daily_limit_usdc": 25_000,
            "is_paused": False,
            "created_at": now,
            "updated_at": now,
        }
    )
    app.state.pool = pool

    for _ in range(2):
        response = await admin_api_client.put(
            "/org/principals/agent-1/spend-limit",
            json={"daily_limit_usdc": 25_000, "is_paused": False},
        )
        assert response.status_code == 200

    assert pool.fetchrow.await_count == 2
    assert pool.fetchrow.call_args.args[1:] == ("test-org-id", "agent-1", 25_000, False)
    assert "ON CONFLICT (org_id, principal_id)" in pool.fetchrow.call_args.args[0]


@pytest.mark.anyio
async def test_delete_principal_limit_is_idempotent_and_org_scoped(admin_api_client):
    from teardrop.main import app

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="DELETE 0")
    app.state.pool = pool

    for _ in range(2):
        response = await admin_api_client.delete("/org/principals/agent-1/spend-limit")
        assert response.status_code == 204

    assert pool.execute.await_count == 2
    assert pool.execute.call_args.args[1:] == ("test-org-id", "agent-1")


@pytest.mark.anyio
async def test_principal_limit_rejects_non_positive_amount(admin_api_client):
    response = await admin_api_client.put(
        "/org/principals/agent-1/spend-limit",
        json={"daily_limit_usdc": 0},
    )
    assert response.status_code == 422
