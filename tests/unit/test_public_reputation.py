"""Unit tests for public marketplace reputation aggregates."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from marketplace.reputation import _load_public_reputation


@pytest.mark.anyio
async def test_load_public_reputation_filters_active_tools_and_suppresses_small_counts(monkeypatch):
    updated_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "qualified_tool_name": "platform/web_search",
                "reputation_score": 0.91,
                "success_rate": 0.96,
                "reputation_sample_size": 12.5,
                "reputation_confidence": 0.71,
                "reputation_freshness": 1.0,
                "average_latency_ms": 85,
                "unique_caller_count": 4,
                "updated_at": updated_at,
            },
            {
                "qualified_tool_name": "acme/oracle",
                "reputation_score": 0.88,
                "success_rate": 0.93,
                "reputation_sample_size": 20,
                "reputation_confidence": 0.8,
                "reputation_freshness": 0.9,
                "average_latency_ms": 120,
                "unique_caller_count": 5,
                "updated_at": updated_at,
            },
            {
                "qualified_tool_name": "acme/unrated",
                "reputation_score": 0,
                "success_rate": 0,
                "reputation_sample_size": 0,
                "reputation_confidence": 0,
                "reputation_freshness": 0,
                "average_latency_ms": 0,
                "unique_caller_count": 0,
                "updated_at": None,
            },
        ]
    )
    monkeypatch.setattr("marketplace.reputation._get_pool", lambda: pool)

    snapshot = await _load_public_reputation()

    assert snapshot["generated_at"] == updated_at.isoformat()
    assert "unique_caller_count" not in snapshot["tools"]["platform/web_search"]
    assert snapshot["tools"]["acme/oracle"]["unique_caller_count"] == 5
    assert snapshot["tools"]["acme/unrated"] == {
        "reputation_score": 0.0,
        "success_rate": 0.0,
        "sample_size": 0.0,
        "confidence": 0.0,
        "freshness": 0.0,
        "average_latency_ms": 0.0,
    }
    sql = pool.fetch.call_args.args[0]
    assert "t.publish_as_mcp = TRUE" in sql
    assert "t.is_active = TRUE" in sql
    assert "p.is_active = TRUE" in sql
    assert "FROM active_tools a" in sql
    assert "LEFT JOIN marketplace_tool_call_stats s USING (qualified_tool_name)" in sql
