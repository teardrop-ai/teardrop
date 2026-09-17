# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for the in-process discovery-stage hit counters."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import teardrop.funnel_counters as funnel_module
from teardrop.funnel_counters import (
    SURFACE_AGENT_CARD,
    SURFACE_CATALOG,
    SURFACE_MCP_402_CHALLENGE,
    close_funnel_counters,
    flush_discovery_counters,
    init_funnel_counters,
    record_discovery_hit,
)


def _pool():
    pool = MagicMock()
    pool.executemany = AsyncMock(return_value=None)
    return pool


@pytest.fixture(autouse=True)
def _reset_counters():
    close_funnel_counters()
    yield
    close_funnel_counters()


class TestRecordDiscoveryHit:
    def test_disabled_counter_is_noop(self):
        record_discovery_hit(SURFACE_AGENT_CARD)
        assert funnel_module._counters == {}

    def test_enabled_counter_increments(self):
        init_funnel_counters(_pool(), enabled=True)
        record_discovery_hit(SURFACE_AGENT_CARD)
        record_discovery_hit(SURFACE_AGENT_CARD)
        record_discovery_hit(SURFACE_CATALOG)

        assert len(funnel_module._counters) == 2
        assert sum(funnel_module._counters.values()) == 3

    def test_unknown_surface_is_ignored(self):
        init_funnel_counters(_pool(), enabled=True)
        record_discovery_hit("not_a_surface")
        record_discovery_hit("")

        assert funnel_module._counters == {}


@pytest.mark.anyio
class TestFlushDiscoveryCounters:
    async def test_flush_upserts_and_clears_counters(self):
        pool = _pool()
        init_funnel_counters(pool, enabled=True)
        record_discovery_hit(SURFACE_AGENT_CARD)
        record_discovery_hit(SURFACE_MCP_402_CHALLENGE)

        flushed = await flush_discovery_counters()

        assert flushed == 2
        assert funnel_module._counters == {}
        sql = pool.executemany.await_args.args[0]
        assert "INSERT INTO discovery_stage_counts" in sql
        assert "ON CONFLICT (surface, bucket_hour)" in sql
        rows = pool.executemany.await_args.args[1]
        assert len(rows) == 2

    async def test_flush_without_pool_or_counters_is_noop(self):
        init_funnel_counters(_pool(), enabled=True)
        assert await flush_discovery_counters() == 0

        close_funnel_counters()
        record_discovery_hit(SURFACE_AGENT_CARD)
        assert await flush_discovery_counters() == 0

    async def test_flush_failure_retains_counters_for_retry(self):
        pool = _pool()
        pool.executemany = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        init_funnel_counters(pool, enabled=True)
        record_discovery_hit(SURFACE_AGENT_CARD)

        assert await flush_discovery_counters() == 0
        assert len(funnel_module._counters) == 1

    async def test_repeated_hits_same_hour_aggregate_into_one_bucket(self):
        pool = _pool()
        init_funnel_counters(pool, enabled=True)
        for _ in range(50):
            record_discovery_hit(SURFACE_AGENT_CARD)

        flushed = await flush_discovery_counters()

        assert flushed == 1
        rows = pool.executemany.await_args.args[1]
        assert len(rows) == 1
        assert rows[0][2] == 50
