"""Unit tests for marketplace.worker.reputation_rollup_once.

All DB interactions are mocked via the same ``marketplace._pool`` monkeypatch
hook used by tests/unit/test_marketplace_sweep.py; no live Postgres required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from marketplace import reputation_rollup_once


class TestReputationRollupOnce:
    @pytest.mark.anyio
    async def test_no_events_returns_zero(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        count = await reputation_rollup_once()

        assert count == 0
        mock_pool.execute.assert_not_called()

    @pytest.mark.anyio
    async def test_upserts_computed_reputation_score(self, monkeypatch):
        rows = [
            {
                "qualified_tool_name": "platform/get_datetime",
                "tool_type": "platform",
                "failures": 2,
                "total_latency_ms": 500,
                "success_rate": 0.8,
                "popularity_norm": 1.0,
            },
            {
                "qualified_tool_name": "acme/weather",
                "tool_type": "community",
                "failures": 0,
                "total_latency_ms": 120,
                "success_rate": 1.0,
                "popularity_norm": 0.1,
            },
        ]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=rows)
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        count = await reputation_rollup_once()

        assert count == 2
        assert mock_pool.execute.call_count == 2

        first_call_args = mock_pool.execute.call_args_list[0].args
        # SQL text, qualified_tool_name, tool_type, failures, total_latency_ms, reputation_score
        assert "ON CONFLICT (qualified_tool_name) DO UPDATE" in first_call_args[0]
        assert "platform/get_datetime" in first_call_args
        assert "platform" in first_call_args
        assert 2 in first_call_args
        assert 500 in first_call_args
        assert round(0.6 * 0.8 + 0.4 * 1.0, 6) in first_call_args

    @pytest.mark.anyio
    async def test_never_writes_total_calls_column(self, monkeypatch):
        """The rollup must never touch total_calls -- that column is owned by
        record_marketplace_tool_call() and must not be overwritten by telemetry."""
        rows = [
            {
                "qualified_tool_name": "platform/get_datetime",
                "tool_type": "platform",
                "failures": 0,
                "total_latency_ms": 10,
                "success_rate": 1.0,
                "popularity_norm": 1.0,
            }
        ]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=rows)
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await reputation_rollup_once()

        sql_text = mock_pool.execute.call_args_list[0].args[0]
        assert "total_calls" not in sql_text

    @pytest.mark.anyio
    async def test_excludes_author_self_calls_from_reputation(self, monkeypatch):
        """An author cannot improve a community tool's score with own-org calls."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await reputation_rollup_once()

        sql_text = mock_pool.fetch.call_args.args[0]
        assert "FROM org_tools t" in sql_text
        assert "JOIN orgs o ON o.id = t.org_id" in sql_text
        assert "JOIN catalog_tools c USING (qualified_tool_name)" in sql_text
        assert "o.slug <> 'platform'" in sql_text
        assert "e.org_id IS DISTINCT FROM c.author_org_id" in sql_text
        assert "marketplace_tool_call_stats s" not in sql_text
