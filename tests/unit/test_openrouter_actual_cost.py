# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""OpenRouter upstream metadata capture and actual-cost billing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent._llm_usage import extract_usage
from agent._openrouter import ChatOpenRouter, upstream_metadata
from billing.pricing import calculate_turns_token_cost_usdc, upstream_turn_cost_usdc

_USAGE = {"prompt_tokens": 32, "completion_tokens": 5, "total_tokens": 37, "cost": 3.78e-06}


def _llm() -> ChatOpenRouter:
    return ChatOpenRouter(model="~deepseek/deepseek-flash-latest", api_key="k", base_url="https://openrouter.ai/api/v1")


def _settings(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        openrouter_actual_cost_billing_enabled=enabled,
        llm_cost_markup=1.25,
        openrouter_credit_fee_rate=0.055,
    )


class TestUpstreamMetadata:
    def test_extracts_cost_model_and_provider(self):
        meta = upstream_metadata({"model": "deepseek/deepseek-v4.1-flash", "provider": "Decart", "usage": _USAGE})
        assert meta == {
            "upstream_cost_usd": 3.78e-06,
            "upstream_model": "deepseek/deepseek-v4.1-flash",
            "upstream_provider": "Decart",
        }

    def test_ignores_missing_or_non_numeric_cost(self):
        assert upstream_metadata({"usage": {"cost": "free"}}) == {}

    def test_non_streaming_result_carries_upstream_metadata(self):
        response = {
            "id": "gen-1",
            "model": "deepseek/deepseek-v4.1-flash",
            "provider": "Decart",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": _USAGE,
        }
        message = _llm()._create_chat_result(response).generations[0].message
        assert message.response_metadata["upstream_cost_usd"] == 3.78e-06
        assert message.response_metadata["upstream_model"] == "deepseek/deepseek-v4.1-flash"

    def test_streaming_usage_chunk_carries_upstream_metadata_once(self):
        llm = _llm()
        content = llm._convert_chunk_to_generation_chunk(
            {
                "model": "deepseek/deepseek-v4.1-flash",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}}],
            },
            AIMessageChunk,
            None,
        )
        usage = llm._convert_chunk_to_generation_chunk(
            {"model": "deepseek/deepseek-v4.1-flash", "provider": "Decart", "choices": [], "usage": _USAGE},
            AIMessageChunk,
            None,
        )
        merged = content.message + usage.message
        assert "upstream_model" not in content.message.response_metadata
        assert merged.response_metadata["upstream_model"] == "deepseek/deepseek-v4.1-flash"
        assert merged.response_metadata["upstream_cost_usd"] == 3.78e-06

    def test_extract_usage_passes_upstream_fields_through(self):
        message = AIMessage(
            content="hi",
            usage_metadata={"input_tokens": 32, "output_tokens": 5, "total_tokens": 37},
            response_metadata={"upstream_cost_usd": 3.78e-06, "upstream_model": "deepseek/deepseek-v4.1-flash"},
        )
        usage = extract_usage(message)
        assert usage["tokens_in"] == 32
        assert usage["upstream_cost_usd"] == 3.78e-06
        assert usage["upstream_model"] == "deepseek/deepseek-v4.1-flash"
        assert "upstream_provider" not in usage


class TestCacheTokenExtraction:
    def test_reads_langchain_input_token_details(self):
        message = AIMessage(
            content="hi",
            usage_metadata={
                "input_tokens": 5000,
                "output_tokens": 10,
                "total_tokens": 5010,
                "input_token_details": {"cache_read": 3000, "ephemeral_5m_input_tokens": 1500, "cache_creation": 0},
            },
        )
        usage = extract_usage(message)
        assert usage["tokens_in"] == 5000
        assert usage["cache_read_input_tokens"] == 3000
        assert usage["cache_creation_input_tokens"] == 1500


class TestActualCostBilling:
    def test_disabled_returns_none(self):
        with patch("billing.pricing.get_settings", return_value=_settings(enabled=False)):
            assert upstream_turn_cost_usdc({"provider": "openrouter", "upstream_cost_usd": 0.01}) is None

    def test_applies_markup_and_credit_fee_rounding_up(self):
        with patch("billing.pricing.get_settings", return_value=_settings()):
            # 0.01 USD * 1.25 * 1.055 = 0.0131875 USD = 13187.5 atomic -> 13188
            assert upstream_turn_cost_usdc({"provider": "openrouter", "upstream_cost_usd": 0.01}) == 13188
            assert upstream_turn_cost_usdc({"provider": "openrouter", "upstream_cost_usd": 3.78e-06}) == 5

    def test_only_openrouter_turns_with_reported_cost(self):
        with patch("billing.pricing.get_settings", return_value=_settings()):
            assert upstream_turn_cost_usdc({"provider": "google", "upstream_cost_usd": 0.01}) is None
            assert upstream_turn_cost_usdc({"provider": "openrouter"}) is None

    @pytest.mark.asyncio
    async def test_mixed_turns_use_cost_or_rule(self):
        rule = SimpleNamespace(
            id="google-rule", run_price_usdc=0, tokens_in_cost_per_1k=100, tokens_out_cost_per_1k=200, tool_call_cost=0
        )
        turns = [
            {"provider": "openrouter", "model": "~a/latest", "tokens_in": 9999, "tokens_out": 9999, "upstream_cost_usd": 0.01},
            {"provider": "google", "model": "gemini", "tokens_in": 2000, "tokens_out": 1000},
        ]
        with (
            patch("billing.pricing.get_settings", return_value=_settings()),
            patch("billing.pricing.get_live_pricing_for_model", new_callable=AsyncMock, return_value=rule) as lookup,
        ):
            total = await calculate_turns_token_cost_usdc(turns)
        assert total == 13188 + (2 * 100 + 1 * 200)
        lookup.assert_awaited_once_with("google", "gemini")
