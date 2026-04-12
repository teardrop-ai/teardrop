"""Integration tests for memory.py — requires Docker Postgres with pgvector.

These tests are skipped if Docker or DATABASE_URL is not available.
Embedding generation is mocked (no OpenAI key needed for CI/local).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import memory as memory_module
import users as user_module
from memory import (
    count_memories,
    delete_all_org_memories,
    delete_memory,
    list_memories,
    recall_memories,
    store_memory,
)
from users import create_org, create_user


@pytest.fixture(autouse=True)
def bind_pools(db_pool):
    user_module._pool = db_pool
    memory_module._pool = db_pool
    yield
    user_module._pool = None
    memory_module._pool = None


@pytest.fixture
async def test_user(db_pool):
    org = await create_org("Memory Org")
    return await create_user("memory_test@example.com", "pass", org.id)


def _mock_embedding(text: str = ""):
    """Return a dummy 1536-dim vector — avoids needing OpenAI credentials."""
    return [0.1] * 1536


def _embed_patch():
    """Return a context manager that patches _generate_embedding."""
    return patch.object(
        memory_module,
        "_generate_embedding",
        AsyncMock(side_effect=lambda t: _mock_embedding(t)),
    )


@pytest.mark.anyio
async def test_store_and_recall(test_user):
    with _embed_patch():
        entry = await store_memory(
            test_user.org_id, test_user.id, "user prefers dark mode",
        )
        assert entry is not None
        assert entry.content == "user prefers dark mode"

        results = await recall_memories(
            test_user.org_id, "dark mode", top_k=3,
        )
        assert len(results) >= 1
        assert results[0].content == "user prefers dark mode"


@pytest.mark.anyio
async def test_list_and_count(test_user):
    with _embed_patch():
        await store_memory(test_user.org_id, test_user.id, "fact one")
        await store_memory(test_user.org_id, test_user.id, "fact two")

        count = await count_memories(test_user.org_id)
        assert count >= 2

        entries = await list_memories(test_user.org_id, limit=10)
        assert len(entries) >= 2


@pytest.mark.anyio
async def test_delete_memory(test_user):
    with _embed_patch():
        entry = await store_memory(test_user.org_id, test_user.id, "to be deleted")
        assert entry is not None

        deleted = await delete_memory(entry.id, test_user.org_id)
        assert deleted is True

        # Double delete returns False.
        deleted2 = await delete_memory(entry.id, test_user.org_id)
        assert deleted2 is False


@pytest.mark.anyio
async def test_delete_memory_scoped_to_org(test_user):
    with _embed_patch():
        entry = await store_memory(test_user.org_id, test_user.id, "org scoped fact")
        assert entry is not None

        # Cannot delete with wrong org_id.
        deleted = await delete_memory(entry.id, "wrong-org-id")
        assert deleted is False


@pytest.mark.anyio
async def test_delete_all_org_memories(test_user):
    with _embed_patch():
        await store_memory(test_user.org_id, test_user.id, "fact a")
        await store_memory(test_user.org_id, test_user.id, "fact b")

        deleted_count = await delete_all_org_memories(test_user.org_id)
        assert deleted_count >= 2

        count = await count_memories(test_user.org_id)
        assert count == 0


@pytest.mark.anyio
async def test_cursor_pagination(test_user):
    with _embed_patch():
        await store_memory(test_user.org_id, test_user.id, "page fact 1")
        await store_memory(test_user.org_id, test_user.id, "page fact 2")
        await store_memory(test_user.org_id, test_user.id, "page fact 3")

        page1 = await list_memories(test_user.org_id, limit=2)
        assert len(page1) == 2

        page2 = await list_memories(test_user.org_id, limit=2, cursor=page1[-1].created_at)
        assert len(page2) >= 1
