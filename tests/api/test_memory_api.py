"""API tests for memory endpoints (GET/POST/DELETE /memories, admin endpoints)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from memory import MemoryEntry

_NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)

_ENTRY = MemoryEntry(
    id="mem-1",
    org_id="test-org-id",
    user_id="test-user-id",
    content="user prefers dark mode",
    source_run_id="run-123",
    created_at=_NOW,
)


# ─── GET /memories ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_memories(api_client, monkeypatch):
    monkeypatch.setattr("app.list_memories", AsyncMock(return_value=[_ENTRY]))
    monkeypatch.setattr("app.count_memories", AsyncMock(return_value=1))

    resp = await api_client.get("/memories")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == "mem-1"
    assert body["items"][0]["content"] == "user prefers dark mode"


@pytest.mark.anyio
async def test_list_memories_with_cursor(api_client, monkeypatch):
    monkeypatch.setattr("app.list_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.count_memories", AsyncMock(return_value=0))

    resp = await api_client.get("/memories", params={"cursor": _NOW.isoformat()})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.anyio
async def test_list_memories_requires_auth(anon_client):
    resp = await anon_client.get("/memories")
    assert resp.status_code == 401


# ─── POST /memories ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_store_memory(api_client, monkeypatch):
    monkeypatch.setattr("app.store_memory", AsyncMock(return_value=_ENTRY))

    resp = await api_client.post("/memories", json={"content": "user prefers dark mode"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "mem-1"


@pytest.mark.anyio
async def test_store_memory_returns_422_on_limit(api_client, monkeypatch):
    monkeypatch.setattr("app.store_memory", AsyncMock(return_value=None))

    resp = await api_client.post("/memories", json={"content": "some fact"})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_store_memory_requires_auth(anon_client):
    resp = await anon_client.post("/memories", json={"content": "fact"})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_store_memory_validates_content(api_client):
    resp = await api_client.post("/memories", json={"content": ""})
    assert resp.status_code == 422


# ─── DELETE /memories/{memory_id} ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_delete_memory(api_client, monkeypatch):
    monkeypatch.setattr("app.delete_memory", AsyncMock(return_value=True))

    resp = await api_client.delete("/memories/mem-1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


@pytest.mark.anyio
async def test_delete_memory_not_found(api_client, monkeypatch):
    monkeypatch.setattr("app.delete_memory", AsyncMock(return_value=False))

    resp = await api_client.delete("/memories/mem-999")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_delete_memory_requires_auth(anon_client):
    resp = await anon_client.delete("/memories/mem-1")
    assert resp.status_code == 401


# ─── GET /admin/memories/org/{org_id} ─────────────────────────────────────────


@pytest.mark.anyio
async def test_admin_list_org_memories(admin_api_client, monkeypatch):
    monkeypatch.setattr("app.list_memories", AsyncMock(return_value=[_ENTRY]))
    monkeypatch.setattr("app.count_memories", AsyncMock(return_value=1))

    resp = await admin_api_client.get("/admin/memories/org/test-org-id")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["user_id"] == "test-user-id"


@pytest.mark.anyio
async def test_admin_list_org_memories_requires_admin(api_client):
    resp = await api_client.get("/admin/memories/org/test-org-id")
    assert resp.status_code == 403


# ─── DELETE /admin/memories/org/{org_id} ──────────────────────────────────────


@pytest.mark.anyio
async def test_admin_purge_org_memories(admin_api_client, monkeypatch):
    monkeypatch.setattr("app.delete_all_org_memories", AsyncMock(return_value=5))

    resp = await admin_api_client.delete("/admin/memories/org/test-org-id")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "purged"
    assert body["deleted"] == 5


@pytest.mark.anyio
async def test_admin_purge_requires_admin(api_client):
    resp = await api_client.delete("/admin/memories/org/test-org-id")
    assert resp.status_code == 403
