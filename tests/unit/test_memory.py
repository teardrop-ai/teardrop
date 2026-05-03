"""Unit tests for memory.py — DB functions mocked via pool MagicMock."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import memory as memory_module
from memory import MemoryEntry

# ─── Pool mock helper ─────────────────────────────────────────────────────────


def _pool():
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


def _mock_embedding():
    """Return a patched _generate_embedding that returns a dummy vector."""
    return AsyncMock(return_value=[0.1] * 1536)


# ─── MemoryEntry model ───────────────────────────────────────────────────────


class TestMemoryEntry:
    def test_defaults(self):
        entry = MemoryEntry(org_id="org-1", user_id="user-1", content="fact")
        assert entry.org_id == "org-1"
        assert entry.content == "fact"
        assert entry.id  # auto-generated UUID
        assert entry.source_run_id is None
        assert isinstance(entry.created_at, datetime)

    def test_explicit_fields(self):
        entry = MemoryEntry(
            id="m-1",
            org_id="org-1",
            user_id="user-1",
            content="some fact",
            source_run_id="run-123",
        )
        assert entry.id == "m-1"
        assert entry.source_run_id == "run-123"


# ─── store_memory ────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestStoreMemory:
    async def test_stores_successfully(self, test_settings):
        pool = _pool()
        # First fetchrow: count_memories returns (5,), second: INSERT RETURNING id
        pool.fetchrow = AsyncMock(side_effect=[(5,), {"id": "m-new"}])
        cache: dict[str, tuple[float, bool]] = {}

        with (
            patch.object(memory_module, "_pool", pool),
            patch.object(memory_module, "_generate_embedding", _mock_embedding()),
            patch.object(memory_module, "_memory_count_cache", cache),
        ):
            entry = await memory_module.store_memory("org-1", "user-1", "a fact")
            assert cache["org-1"][1] is True

        assert entry is not None
        assert entry.content == "a fact"
        assert pool.fetchrow.call_count == 2

    async def test_returns_none_when_limit_reached(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=(1000,))  # at default limit

        with (
            patch.object(memory_module, "_pool", pool),
            patch.object(memory_module, "_generate_embedding", _mock_embedding()),
        ):
            entry = await memory_module.store_memory("org-1", "user-1", "new fact")

        assert entry is None
        # Only the count_memories fetchrow should have been called
        assert pool.fetchrow.call_count == 1

    async def test_truncates_content_to_500_chars(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(side_effect=[(0,), {"id": "m-trunc"}])

        with (
            patch.object(memory_module, "_pool", pool),
            patch.object(memory_module, "_generate_embedding", _mock_embedding()),
        ):
            long_content = "x" * 600
            entry = await memory_module.store_memory("org-1", "user-1", long_content)

        assert entry is not None
        assert len(entry.content) == 500

    async def test_swallows_exceptions(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(side_effect=[(0,), Exception("DB error")])

        with (
            patch.object(memory_module, "_pool", pool),
            patch.object(memory_module, "_generate_embedding", _mock_embedding()),
        ):
            entry = await memory_module.store_memory("org-1", "user-1", "fact")

        assert entry is None


# ─── recall_memories ──────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestRecallMemories:
    async def test_returns_entries(self, test_settings):
        pool = _pool()
        now = datetime.now(timezone.utc)
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "id": "m-1",
                    "org_id": "org-1",
                    "user_id": "user-1",
                    "content": "fact one",
                    "source_run_id": None,
                    "created_at": now,
                }
            ]
        )

        with (
            patch.object(memory_module, "_pool", pool),
            patch.object(memory_module, "_generate_embedding", _mock_embedding()),
            patch.object(memory_module, "has_memories_cached", AsyncMock(return_value=True)),
        ):
            results = await memory_module.recall_memories("org-1", "some query")

        assert len(results) == 1
        assert results[0].content == "fact one"

    async def test_returns_empty_on_error(self, test_settings):
        pool = _pool()
        pool.fetch = AsyncMock(side_effect=Exception("DB error"))

        with (
            patch.object(memory_module, "_pool", pool),
            patch.object(memory_module, "_generate_embedding", _mock_embedding()),
            patch.object(memory_module, "has_memories_cached", AsyncMock(return_value=True)),
        ):
            results = await memory_module.recall_memories("org-1", "query")

        assert results == []

    async def test_skips_embedding_when_org_has_no_memories(self, test_settings):
        pool = _pool()
        embed_mock = _mock_embedding()

        with (
            patch.object(memory_module, "_pool", pool),
            patch.object(memory_module, "_generate_embedding", embed_mock),
            patch.object(memory_module, "has_memories_cached", AsyncMock(return_value=False)),
        ):
            results = await memory_module.recall_memories("org-1", "query")

        assert results == []
        embed_mock.assert_not_called()
        pool.fetch.assert_not_called()


# ─── has_memories_cached ─────────────────────────────────────────────────────


@pytest.mark.anyio
class TestHasMemoriesCached:
    async def test_returns_false_when_count_is_zero(self, test_settings):
        with (
            patch.object(memory_module, "_memory_count_cache", {}),
            patch.object(memory_module, "count_memories", AsyncMock(return_value=0)),
        ):
            result = await memory_module.has_memories_cached("org-1")

        assert result is False

    async def test_uses_cached_value_before_ttl_expires(self, test_settings):
        with (
            patch.object(memory_module, "_memory_count_cache", {"org-1": (9999.0, True)}),
            patch("memory.time.monotonic", return_value=1000.0),
            patch.object(memory_module, "count_memories", AsyncMock(return_value=0)) as count_mock,
        ):
            result = await memory_module.has_memories_cached("org-1")

        assert result is True
        count_mock.assert_not_called()

    async def test_refreshes_cache_after_ttl_expiry(self, test_settings):
        with (
            patch.object(memory_module, "_memory_count_cache", {"org-1": (1000.0, False)}),
            patch("memory.time.monotonic", return_value=2000.0),
            patch.object(memory_module, "count_memories", AsyncMock(return_value=3)) as count_mock,
        ):
            result = await memory_module.has_memories_cached("org-1")

        assert result is True
        count_mock.assert_called_once_with("org-1")


# ─── list_memories ────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestListMemories:
    async def test_returns_entries_without_cursor(self, test_settings):
        pool = _pool()
        now = datetime.now(timezone.utc)
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "id": "m-1",
                    "org_id": "org-1",
                    "user_id": "user-1",
                    "content": "fact",
                    "source_run_id": None,
                    "created_at": now,
                }
            ]
        )

        with patch.object(memory_module, "_pool", pool):
            results = await memory_module.list_memories("org-1")

        assert len(results) == 1

    async def test_returns_entries_with_cursor(self, test_settings):
        pool = _pool()
        pool.fetch = AsyncMock(return_value=[])

        cursor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(memory_module, "_pool", pool):
            results = await memory_module.list_memories("org-1", cursor=cursor)

        assert results == []
        call_args = pool.fetch.call_args.args
        assert cursor in call_args


# ─── count_memories ───────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCountMemories:
    async def test_returns_count(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=(42,))

        with patch.object(memory_module, "_pool", pool):
            count = await memory_module.count_memories("org-1")

        assert count == 42

    async def test_returns_zero_when_no_row(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)

        with patch.object(memory_module, "_pool", pool):
            count = await memory_module.count_memories("org-1")

        assert count == 0


# ─── delete_memory ────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestDeleteMemory:
    async def test_returns_true_on_success(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(return_value="DELETE 1")

        with patch.object(memory_module, "_pool", pool):
            deleted = await memory_module.delete_memory("m-1", "org-1")

        assert deleted is True

    async def test_returns_false_when_not_found(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(return_value="DELETE 0")

        with patch.object(memory_module, "_pool", pool):
            deleted = await memory_module.delete_memory("m-99", "org-1")

        assert deleted is False


# ─── delete_all_org_memories ──────────────────────────────────────────────────


@pytest.mark.anyio
class TestDeleteAllOrgMemories:
    async def test_returns_count(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(return_value="DELETE 5")

        with patch.object(memory_module, "_pool", pool):
            count = await memory_module.delete_all_org_memories("org-1")

        assert count == 5

    async def test_returns_zero_when_none_deleted(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(return_value="DELETE 0")

        with patch.object(memory_module, "_pool", pool):
            count = await memory_module.delete_all_org_memories("org-1")

        assert count == 0


# ─── _extract_facts ───────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestExtractFacts:
    async def test_extracts_facts(self, test_settings):
        mock_response = MagicMock()
        mock_response.content = json.dumps({"facts": ["fact one", "fact two"]})
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        msgs = [MagicMock(type="human", content="My wallet is 0xABC")]

        with patch("agent.llm.get_llm", return_value=mock_llm):
            facts = await memory_module._extract_facts(msgs)

        assert facts == ["fact one", "fact two"]

    async def test_handles_markdown_fenced_json(self, test_settings):
        mock_response = MagicMock()
        mock_response.content = '```json\n{"facts": ["fenced fact"]}\n```'
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        msgs = [MagicMock(type="human", content="some text")]

        with patch("agent.llm.get_llm", return_value=mock_llm):
            facts = await memory_module._extract_facts(msgs)

        assert facts == ["fenced fact"]

    async def test_returns_empty_on_error(self, test_settings):
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("LLM down"))

        msgs = [MagicMock(type="human", content="text")]

        with patch("agent.llm.get_llm", return_value=mock_llm):
            facts = await memory_module._extract_facts(msgs)

        assert facts == []

    async def test_returns_empty_for_empty_messages(self, test_settings):
        facts = await memory_module._extract_facts([])
        assert facts == []

    async def test_limits_to_five_facts(self, test_settings):
        mock_response = MagicMock()
        mock_response.content = json.dumps({"facts": [f"fact {i}" for i in range(10)]})
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        msgs = [MagicMock(type="human", content="text")]

        with patch("agent.llm.get_llm", return_value=mock_llm):
            facts = await memory_module._extract_facts(msgs)

        assert len(facts) == 5


# ─── extract_and_store_memories ───────────────────────────────────────────────


@pytest.mark.anyio
class TestExtractAndStoreMemories:
    async def test_extracts_and_stores(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=(0,))

        with (
            patch.object(memory_module, "_pool", pool),
            patch.object(memory_module, "_generate_embedding", _mock_embedding()),
            patch.object(
                memory_module,
                "_extract_facts",
                AsyncMock(return_value=["fact one", "fact two"]),
            ),
        ):
            count = await memory_module.extract_and_store_memories("org-1", "user-1", [], "run-1")

        assert count == 2

    async def test_skips_extraction_for_stateless_lookup_runs(self, test_settings):
        with (
            patch.object(memory_module, "_is_stateless_lookup_run", return_value=True),
            patch.object(memory_module, "_extract_facts", AsyncMock(return_value=["fact one"])) as extract_mock,
        ):
            count = await memory_module.extract_and_store_memories(
                "org-1",
                "user-1",
                [MagicMock(type="human", content="show me btc performance")],
                "run-1",
                tool_names_used=["get_token_price_historical"],
            )

        assert count == 0
        extract_mock.assert_not_called()


# ─── _is_stateless_lookup_run ───────────────────────────────────────────────


class TestIsStatelessLookupRun:
    def test_true_for_simple_lookup(self):
        msgs = [MagicMock(type="human", content="Show BTC monthly change")]
        assert memory_module._is_stateless_lookup_run(msgs, ["get_token_price_historical"]) is True

    def test_false_when_wallet_address_present(self):
        msgs = [MagicMock(type="human", content="check 0x1234567890abcdef1234567890abcdef12345678")]
        assert memory_module._is_stateless_lookup_run(msgs, ["get_token_price_historical"]) is False

    def test_false_when_non_stateless_tool_used(self):
        msgs = [MagicMock(type="human", content="analyze this")]
        assert memory_module._is_stateless_lookup_run(msgs, ["web_search"]) is False


# ─── init_memory_db / close_memory_db / _get_pool ────────────────────────────


@pytest.mark.anyio
class TestInitCloseGetPool:
    async def test_init_when_memory_disabled(self, test_settings, monkeypatch):
        import config

        monkeypatch.setenv("MEMORY_ENABLED", "false")
        config.get_settings.cache_clear()
        pool = _pool()
        with patch.object(memory_module, "_pool", None):
            await memory_module.init_memory_db(pool)
            assert memory_module._pool is pool
        config.get_settings.cache_clear()

    async def test_init_when_no_openai_key(self, test_settings, monkeypatch):
        import config

        monkeypatch.setenv("OPENAI_API_KEY", "")
        config.get_settings.cache_clear()
        pool = _pool()
        with patch.object(memory_module, "_pool", None):
            await memory_module.init_memory_db(pool)
            assert memory_module._pool is pool
        config.get_settings.cache_clear()

    async def test_close_releases_pool(self, test_settings):
        with patch.object(memory_module, "_pool", _pool()):
            await memory_module.close_memory_db()
        assert memory_module._pool is None

    async def test_close_is_noop_when_none(self, test_settings):
        with patch.object(memory_module, "_pool", None):
            await memory_module.close_memory_db()  # should not raise

    def test_get_pool_raises_when_uninitialised(self, test_settings):
        with patch.object(memory_module, "_pool", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                memory_module._get_pool()


# ─── cleanup_expired_memories ────────────────────────────────────────────────


@pytest.mark.anyio
class TestCleanupExpiredMemories:
    async def test_returns_count(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(return_value="DELETE 7")
        with patch.object(memory_module, "_pool", pool):
            count = await memory_module.cleanup_expired_memories()
        assert count == 7

    async def test_handles_malformed_result(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(return_value="OK")
        with patch.object(memory_module, "_pool", pool):
            count = await memory_module.cleanup_expired_memories()
        assert count == 0

    async def test_returns_zero_on_no_facts(self, test_settings):
        with patch.object(memory_module, "_extract_facts", AsyncMock(return_value=[])):
            count = await memory_module.extract_and_store_memories("org-1", "user-1", [], "run-1")

        assert count == 0

    async def test_never_raises(self, test_settings):
        with patch.object(
            memory_module,
            "_extract_facts",
            AsyncMock(side_effect=Exception("boom")),
        ):
            count = await memory_module.extract_and_store_memories("org-1", "user-1", [], "run-1")

        assert count == 0
