# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""API tests for the persisted tool-exclusion surface:
GET/POST /agent/tool-exclusions and DELETE /agent/tool-exclusions/{tool_name}.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.anyio
async def test_get_tool_exclusions(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.agent.list_org_tool_exclusions",
        AsyncMock(return_value=["web_search", "get_block"]),
    )
    resp = await api_client.get("/agent/tool-exclusions")
    assert resp.status_code == 200
    assert resp.json()["tool_names"] == ["web_search", "get_block"]


@pytest.mark.anyio
async def test_get_tool_exclusions_requires_auth(anon_client):
    resp = await anon_client.get("/agent/tool-exclusions")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_create_tool_exclusion(api_client, monkeypatch):
    mock_add = AsyncMock()
    monkeypatch.setattr("teardrop.routers.agent.add_org_tool_exclusion", mock_add)

    resp = await api_client.post("/agent/tool-exclusions", json={"tool_name": "platform/web_search"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "added", "tool_name": "web_search"}
    mock_add.assert_awaited_once()
    args = mock_add.await_args.args
    assert args[1] == "web_search"


@pytest.mark.anyio
async def test_create_tool_exclusion_rejects_quota_exceeded(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.agent.add_org_tool_exclusion",
        AsyncMock(side_effect=ValueError("Tool exclusion limit reached (50)")),
    )
    resp = await api_client.post("/agent/tool-exclusions", json={"tool_name": "web_search"})
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_create_tool_exclusion_rejects_empty_name(api_client):
    resp = await api_client.post("/agent/tool-exclusions", json={"tool_name": ""})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_delete_tool_exclusion(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.remove_org_tool_exclusion", AsyncMock(return_value=True))
    resp = await api_client.delete("/agent/tool-exclusions/web_search")
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"


@pytest.mark.anyio
async def test_delete_tool_exclusion_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.remove_org_tool_exclusion", AsyncMock(return_value=False))
    resp = await api_client.delete("/agent/tool-exclusions/web_search")
    assert resp.status_code == 404
