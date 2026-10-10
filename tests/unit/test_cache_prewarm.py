# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.cache_prewarm import prewarm_org_prefix


@pytest.mark.anyio
async def test_prewarm_returns_usage_when_probe_succeeds(test_settings):
    msg = MagicMock()
    msg.usage_metadata = {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 10}

    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)

    with patch("agent.cache_prewarm.create_llm_from_config", return_value=llm):
        usage = await prewarm_org_prefix("org-1", "openai", "gpt-4o-mini", llm_config={"api_key": "k"})

    assert usage["tokens_in"] == 10
    assert usage["cache_creation_input_tokens"] == 10


@pytest.mark.anyio
async def test_prewarm_skips_when_no_key(test_settings):
    with patch.object(test_settings, "openai_api_key", ""):
        usage = await prewarm_org_prefix("org-1", "openai", "gpt-4o-mini", llm_config=None)
    assert usage["tokens_in"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("changed_model", [False, True])
async def test_background_prewarm_skips_rejected_or_stale_model(test_settings, changed_model):
    from teardrop import _background_tasks
    from teardrop.llm_config import UnpricedModelError

    pool = AsyncMock()
    pool.fetch.return_value = [{"org_id": "org-1", "provider": "openrouter", "model": "~anthropic/claude-opus-latest"}]
    resolver = AsyncMock(
        return_value={"provider": "openrouter", "model": "~deepseek/deepseek-flash-latest"},
        side_effect=None if changed_model else UnpricedModelError("No pricing rule"),
    )
    probe = AsyncMock(return_value={})
    with (
        patch.object(_background_tasks, "settings", test_settings),
        patch.object(test_settings, "agent_cache_prewarm_enabled", True),
        patch.object(_background_tasks, "resolve_llm_config", resolver),
        patch.object(_background_tasks, "prewarm_org_prefix", probe),
    ):
        await _background_tasks._prewarm_cache_prefixes(pool)
    probe.assert_not_awaited()
