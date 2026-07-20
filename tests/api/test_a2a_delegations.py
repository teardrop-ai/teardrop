"""API coverage for org-scoped A2A delegation history."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest


@pytest.mark.anyio
async def test_list_delegations_returns_task_type(api_client, monkeypatch):
    import billing

    delegation_events = AsyncMock(
        return_value=[
            {
                "id": "delegation-1",
                "run_id": "run-1",
                "agent_url": "https://agent.example.com",
                "agent_name": "Research Agent",
                "task_status": "completed",
                "task_type": "research",
                "cost_usdc": 12_000,
                "billing_method": "credit",
                "settlement_tx": "",
                "error": "",
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
        ]
    )
    monkeypatch.setattr(billing, "get_delegation_events", delegation_events)

    response = await api_client.get("/a2a/delegations")

    assert response.status_code == 200
    assert response.json()[0]["task_type"] == "research"
    delegation_events.assert_awaited_once_with("test-org-id", limit=50)
