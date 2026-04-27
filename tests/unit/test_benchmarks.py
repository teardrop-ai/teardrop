"""Unit tests for benchmarks.py — model catalogue and benchmark response building."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_all_entries_have_default_latency_ms(self):
        for key, entry in MODEL_CATALOGUE.items():
            assert "default_latency_ms" in entry, f"{key} missing default_latency_ms"
            assert entry["default_latency_ms"] > 0, f"{key} default_latency_ms must be > 0"


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


# ─── DB pool helpers ──────────────────────────────────────────────────────────


def _pool():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    return pool


@pytest.mark.anyio
class TestBenchmarksDbHelpers:
    async def test_init_sets_pool(self):
        import benchmarks as bm_module

        pool = _pool()
        with patch.object(bm_module, "_pool", None):
            await bm_module.init_benchmarks_db(pool)
            assert bm_module._pool is pool

    async def test_close_clears_pool(self):
        import benchmarks as bm_module

        with patch.object(bm_module, "_pool", _pool()):
            await bm_module.close_benchmarks_db()
        assert bm_module._pool is None

    def test_get_pool_raises_when_uninitialised(self):
        import benchmarks as bm_module

        with patch.object(bm_module, "_pool", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                bm_module._get_pool()


# ─── get_model_context_specs ──────────────────────────────────────────────────


class TestGetModelContextSpecs:
    def test_known_model(self):
        from benchmarks import get_model_context_specs

        specs = get_model_context_specs("anthropic", "claude-sonnet-4-20250514")
        assert specs["context_window"] == 200_000
        assert specs["supports_tools"] is True

    def test_unknown_model_returns_defaults(self):
        from benchmarks import _DEFAULT_MODEL_SPECS, get_model_context_specs

        specs = get_model_context_specs("unknown", "mystery-model")
        assert specs["context_window"] == _DEFAULT_MODEL_SPECS["context_window"]


# ─── get_model_benchmarks ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestGetModelBenchmarks:
    async def test_scoped_org_bypasses_cache(self):
        import benchmarks as bm_module

        pool = _pool()
        pool.fetch = AsyncMock(return_value=[])
        with patch.object(bm_module, "_pool", pool):
            result = await bm_module.get_model_benchmarks(org_id="org-1")
        assert isinstance(result, dict)
        pool.fetch.assert_called_once()

    async def test_global_returns_empty_when_no_pool(self):
        import benchmarks as bm_module

        with (
            patch.object(bm_module, "_pool", None),
            patch.object(bm_module, "_benchmark_cache", None),
            patch.object(bm_module, "_benchmark_cache_expires", 0.0),
            patch("benchmarks.get_redis", return_value=None),
        ):
            result = await bm_module.get_model_benchmarks()
        assert result == {}

    async def test_global_uses_in_process_cache(self):
        import time

        import benchmarks as bm_module

        cached = {"anthropic:test-model": {"total_runs_7d": 50}}
        with (
            patch.object(bm_module, "_benchmark_cache", cached),
            patch.object(bm_module, "_benchmark_cache_expires", time.monotonic() + 9999),
            patch("benchmarks.get_redis", return_value=None),
        ):
            result = await bm_module.get_model_benchmarks()
        assert result == cached

    async def test_query_benchmarks_returns_result(self):
        import benchmarks as bm_module

        pool = _pool()
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-20250514",
                    "total_runs": 100,
                    "avg_latency_ms": 1800.0,
                    "p95_latency_ms": 3500.0,
                    "avg_cost_usdc": 25000.0,
                    "avg_tokens_per_sec": 40.0,
                }
            ]
        )
        with patch.object(bm_module, "_pool", pool):
            result = await bm_module._query_benchmarks()
        assert "anthropic:claude-sonnet-4-20250514" in result
        assert result["anthropic:claude-sonnet-4-20250514"]["total_runs_7d"] == 100

    async def test_query_benchmarks_handles_null_metrics(self):
        import benchmarks as bm_module

        pool = _pool()
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "total_runs": 50,
                    "avg_latency_ms": None,
                    "p95_latency_ms": None,
                    "avg_cost_usdc": None,
                    "avg_tokens_per_sec": None,
                }
            ]
        )
        with patch.object(bm_module, "_pool", pool):
            result = await bm_module._query_benchmarks()
        entry = result["openai:gpt-4o"]
        assert entry["avg_latency_ms"] is None

    async def test_global_caches_result_from_db(self):
        import benchmarks as bm_module

        pool = _pool()
        pool.fetch = AsyncMock(return_value=[])
        with (
            patch.object(bm_module, "_pool", pool),
            patch.object(bm_module, "_benchmark_cache", None),
            patch.object(bm_module, "_benchmark_cache_expires", 0.0),
            patch("benchmarks.get_redis", return_value=None),
        ):
            result = await bm_module.get_model_benchmarks()
        assert isinstance(result, dict)
        pool.fetch.assert_called_once()
