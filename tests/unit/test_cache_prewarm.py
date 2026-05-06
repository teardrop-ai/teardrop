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
