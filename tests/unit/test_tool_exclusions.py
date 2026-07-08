# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for teardrop/tool_exclusions.py — persisted per-org tool exclusions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import teardrop.tool_exclusions as tool_exclusions_module
from teardrop.tool_exclusions import (
    MAX_EXCLUSIONS_PER_ORG,
    add_org_tool_exclusion,
    close_tool_exclusions_db,
    init_tool_exclusions_db,
    list_org_tool_exclusions,
    remove_org_tool_exclusion,
)


class TestInitClose:
    async def test_init_sets_pool(self):
        pool = MagicMock()
        saved = tool_exclusions_module._pool
        try:
            await init_tool_exclusions_db(pool)
            assert tool_exclusions_module._pool is pool
        finally:
            tool_exclusions_module._pool = saved

    async def test_close_clears_pool(self):
        saved = tool_exclusions_module._pool
        try:
            tool_exclusions_module._pool = MagicMock()
            await close_tool_exclusions_db()
            assert tool_exclusions_module._pool is None
        finally:
            tool_exclusions_module._pool = saved

    def test_get_pool_raises_when_uninitialised(self):
        from teardrop.tool_exclusions import _get_pool

        with patch.object(tool_exclusions_module, "_pool", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                _get_pool()


class TestListOrgToolExclusions:
    async def test_empty_org_id_returns_empty_list(self):
        assert await list_org_tool_exclusions("") == []

    async def test_returns_tool_names(self):
        pool = MagicMock()
        pool.fetch = AsyncMock(return_value=[{"tool_name": "web_search"}, {"tool_name": "get_block"}])
        with patch.object(tool_exclusions_module, "_pool", pool):
            result = await list_org_tool_exclusions("org-1")
        assert result == ["web_search", "get_block"]

    async def test_db_failure_returns_empty_list_never_raises(self):
        pool = MagicMock()
        pool.fetch = AsyncMock(side_effect=Exception("db down"))
        with patch.object(tool_exclusions_module, "_pool", pool):
            result = await list_org_tool_exclusions("org-1")
        assert result == []


class TestAddOrgToolExclusion:
    async def test_inserts_row(self):
        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=0)
        pool.execute = AsyncMock()
        with patch.object(tool_exclusions_module, "_pool", pool):
            await add_org_tool_exclusion("org-1", "web_search")
        pool.execute.assert_awaited_once()

    async def test_raises_when_quota_exceeded(self):
        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=MAX_EXCLUSIONS_PER_ORG)
        pool.execute = AsyncMock()
        with patch.object(tool_exclusions_module, "_pool", pool):
            with pytest.raises(ValueError, match="limit reached"):
                await add_org_tool_exclusion("org-1", "web_search")
        pool.execute.assert_not_awaited()


class TestRemoveOrgToolExclusion:
    async def test_returns_true_when_row_removed(self):
        pool = MagicMock()
        pool.execute = AsyncMock(return_value="DELETE 1")
        with patch.object(tool_exclusions_module, "_pool", pool):
            result = await remove_org_tool_exclusion("org-1", "web_search")
        assert result is True

    async def test_returns_false_when_no_row_removed(self):
        pool = MagicMock()
        pool.execute = AsyncMock(return_value="DELETE 0")
        with patch.object(tool_exclusions_module, "_pool", pool):
            result = await remove_org_tool_exclusion("org-1", "web_search")
        assert result is False
