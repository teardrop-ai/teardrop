"""Unit tests for benchmarks.py — model catalogue and benchmark response building."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from benchmarks import MODEL_CATALOGUE, build_benchmarks_response


class TestModelCatalogue:
    def test_has_expected_models(self):
        assert "anthropic:claude-haiku-4-5-20251001" in MODEL_CATALOGUE
        assert "openai:gpt-4o-mini" in MODEL_CATALOGUE
        assert "google:gemini-2.0-flash" in MODEL_CATALOGUE

    def test_all_entries_have_required_fields(self):
        for key, entry in MODEL_CATALOGUE.items():
            assert "provider" in entry
            assert "model" in entry
            assert "context_window" in entry
            assert "supports_tools" in entry
            assert "quality_tier" in entry

    def test_keys_match_provider_model(self):
        for key, entry in MODEL_CATALOGUE.items():
            assert key == f"{entry['provider']}:{entry['model']}"


class TestBuildBenchmarksResponse:
    @pytest.mark.asyncio
    async def test_returns_all_catalogue_models(self):
        with (
            patch("benchmarks.get_model_benchmarks", new_callable=AsyncMock, return_value={}),
            patch("billing.get_live_pricing_for_model", new_callable=AsyncMock, return_value=None),
        ):
            result = await build_benchmarks_response()
            assert "models" in result
            assert "updated_at" in result
            assert len(result["models"]) == len(MODEL_CATALOGUE)

    @pytest.mark.asyncio
    async def test_attaches_benchmarks_when_available(self):
        benchmarks = {
            "anthropic:claude-haiku-4-5-20251001": {
                "total_runs_7d": 100,
                "avg_latency_ms": 500.0,
                "p95_latency_ms": 1200.0,
                "avg_cost_usdc_per_run": 15.0,
                "avg_tokens_per_sec": 45.0,
            }
        }
        with (
            patch("benchmarks.get_model_benchmarks", new_callable=AsyncMock, return_value=benchmarks),
            patch("billing.get_live_pricing_for_model", new_callable=AsyncMock, return_value=None),
        ):
            result = await build_benchmarks_response()
            haiku = next(m for m in result["models"] if m["model"] == "claude-haiku-4-5-20251001")
            assert "benchmarks" in haiku
            assert haiku["benchmarks"]["total_runs_7d"] == 100

    @pytest.mark.asyncio
    async def test_models_without_benchmarks_have_no_key(self):
        with (
            patch("benchmarks.get_model_benchmarks", new_callable=AsyncMock, return_value={}),
            patch("billing.get_live_pricing_for_model", new_callable=AsyncMock, return_value=None),
        ):
            result = await build_benchmarks_response()
            for model in result["models"]:
                assert "benchmarks" not in model
