"""Unit tests for llm_config.py — CRUD, encryption, caching, and routing."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

import llm_config
from llm_config import (
    OrgLlmConfig,
    _COOLDOWN_SECONDS,
    _decrypt_llm_key,
    _encrypt_llm_key,
    _resolve_shared_key,
    _row_to_config,
    _select_cheapest,
    _select_fastest,
    _select_highest_quality,
    build_llm_config_dict,
    get_org_llm_config,
    get_org_llm_config_cached,
    invalidate_llm_config_cache,
    is_provider_cooled_down,
    record_provider_failure,
    reset_llm_fernet,
    resolve_llm_config,
    upsert_org_llm_config,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear all module-level caches/singletons between tests."""
    reset_llm_fernet()
    llm_config._config_cache.clear()
    llm_config._provider_cooldowns.clear()
    llm_config._config_lock = None
    old_pool = llm_config._pool
    yield
    reset_llm_fernet()
    llm_config._config_cache.clear()
    llm_config._provider_cooldowns.clear()
    llm_config._config_lock = None
    llm_config._pool = old_pool


@pytest.fixture()
def fernet_key():
    return Fernet.generate_key().decode()


@pytest.fixture()
def mock_settings(fernet_key):
    return MagicMock(
        llm_config_encryption_key=fernet_key,
        org_tool_encryption_key="",
        org_tools_cache_ttl_seconds=60,
        anthropic_api_key="shared-anthropic-key",
        openai_api_key="shared-openai-key",
        google_api_key="shared-google-key",
        agent_max_tokens=4096,
        agent_temperature=0.0,
        agent_llm_timeout_seconds=120,
        default_model_pool=[
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "google", "model": "gemini-2.0-flash"},
        ],
    )


def _make_row(
    org_id: str = "org-1",
    provider: str = "anthropic",
    model: str = "claude-haiku-4-5-20251001",
    api_key_enc: str | None = None,
    api_base: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    timeout_seconds: int = 120,
    routing_preference: str = "default",
    is_byok: bool = False,
) -> dict:
    """Simulate an asyncpg Record as a dict (supports __getitem__)."""
    now = datetime.now(timezone.utc)
    return {
        "org_id": org_id,
        "provider": provider,
        "model": model,
        "api_key_enc": api_key_enc,
        "api_base": api_base,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        "routing_preference": routing_preference,
        "is_byok": is_byok,
        "created_at": now,
        "updated_at": now,
    }


# ─── Encryption ───────────────────────────────────────────────────────────────


class TestEncryption:
    def test_round_trip(self, mock_settings):
        with patch("llm_config.get_settings", return_value=mock_settings):
            encrypted = _encrypt_llm_key("sk-test-12345")
            assert encrypted != "sk-test-12345"
            assert _decrypt_llm_key(encrypted) == "sk-test-12345"

    def test_uses_llm_key_over_org_tool_key(self, fernet_key):
        """LLM-specific key takes precedence over org_tool_encryption_key."""
        other_key = Fernet.generate_key().decode()
        settings = MagicMock(
            llm_config_encryption_key=fernet_key,
            org_tool_encryption_key=other_key,
        )
        with patch("llm_config.get_settings", return_value=settings):
            encrypted = _encrypt_llm_key("test")
            # Should decrypt with the LLM key, not the org_tool key
            f = Fernet(fernet_key.encode())
            assert f.decrypt(encrypted.encode()).decode() == "test"

    def test_falls_back_to_org_tool_key(self):
        """Falls back to org_tool_encryption_key when llm key is empty."""
        fallback_key = Fernet.generate_key().decode()
        settings = MagicMock(
            llm_config_encryption_key="",
            org_tool_encryption_key=fallback_key,
        )
        with patch("llm_config.get_settings", return_value=settings):
            encrypted = _encrypt_llm_key("test")
            f = Fernet(fallback_key.encode())
            assert f.decrypt(encrypted.encode()).decode() == "test"

    def test_raises_when_no_keys_configured(self):
        settings = MagicMock(
            llm_config_encryption_key="",
            org_tool_encryption_key="",
        )
        with patch("llm_config.get_settings", return_value=settings):
            with pytest.raises(RuntimeError, match="Cannot encrypt/decrypt"):
                _encrypt_llm_key("test")

    def test_decrypt_with_wrong_key_raises(self, mock_settings):
        """Decrypting with a different key raises an error."""
        with patch("llm_config.get_settings", return_value=mock_settings):
            encrypted = _encrypt_llm_key("test")

        # Reset and use a different key
        reset_llm_fernet()
        wrong_settings = MagicMock(
            llm_config_encryption_key=Fernet.generate_key().decode(),
            org_tool_encryption_key="",
        )
        with patch("llm_config.get_settings", return_value=wrong_settings):
            with pytest.raises(Exception):
                _decrypt_llm_key(encrypted)


# ─── Row → Config mapping ────────────────────────────────────────────────────


class TestRowToConfig:
    def test_maps_all_fields(self):
        row = _make_row(api_key_enc="encrypted-data", is_byok=True, api_base="https://my.endpoint.com")
        cfg = _row_to_config(row)
        assert cfg.org_id == "org-1"
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-haiku-4-5-20251001"
        assert cfg.has_api_key is True
        assert cfg.api_base == "https://my.endpoint.com"
        assert cfg.is_byok is True
        assert cfg.routing_preference == "default"

    def test_no_api_key(self):
        row = _make_row(api_key_enc=None)
        cfg = _row_to_config(row)
        assert cfg.has_api_key is False
        assert cfg.is_byok is False


# ─── build_llm_config_dict ───────────────────────────────────────────────────


class TestBuildLlmConfigDict:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_config(self, mock_settings):
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings):
            result = await build_llm_config_dict("org-no-config")
        assert result is None

    @pytest.mark.asyncio
    async def test_shared_key_fallback(self, mock_settings):
        """Non-BYOK org without api_key_enc uses shared key."""
        row = _make_row(provider="openai", model="gpt-4o-mini")
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=row)
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings):
            result = await build_llm_config_dict("org-1")

        assert result["provider"] == "openai"
        assert result["model"] == "gpt-4o-mini"
        assert result["api_key"] == "shared-openai-key"
        assert result["api_base"] is None

    @pytest.mark.asyncio
    async def test_byok_decrypt_success(self, mock_settings):
        """BYOK org with valid encrypted key decrypts successfully."""
        with patch("llm_config.get_settings", return_value=mock_settings):
            enc = _encrypt_llm_key("byok-real-key")

        row = _make_row(api_key_enc=enc, is_byok=True)
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=row)
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings):
            result = await build_llm_config_dict("org-1")

        assert result["api_key"] == "byok-real-key"

    @pytest.mark.asyncio
    async def test_byok_decrypt_failure_raises(self, mock_settings):
        """BYOK org with corrupted key raises RuntimeError — no silent fallback."""
        row = _make_row(api_key_enc="corrupted-garbage", is_byok=True)
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=row)
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings):
            with pytest.raises(RuntimeError, match="BYOK API key could not be decrypted"):
                await build_llm_config_dict("org-1")

    @pytest.mark.asyncio
    async def test_non_byok_decrypt_failure_falls_back(self, mock_settings):
        """Non-BYOK org with corrupted key falls back to shared key."""
        row = _make_row(api_key_enc="corrupted-garbage", is_byok=False, provider="google")
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=row)
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings):
            result = await build_llm_config_dict("org-1")

        assert result["api_key"] == "shared-google-key"


# ─── resolve_shared_key ──────────────────────────────────────────────────────


class TestResolveSharedKey:
    def test_known_providers(self, mock_settings):
        assert _resolve_shared_key("anthropic", mock_settings) == "shared-anthropic-key"
        assert _resolve_shared_key("openai", mock_settings) == "shared-openai-key"
        assert _resolve_shared_key("google", mock_settings) == "shared-google-key"

    def test_unknown_provider_returns_empty(self, mock_settings):
        assert _resolve_shared_key("cohere", mock_settings) == ""


# ─── Cooldowns ────────────────────────────────────────────────────────────────


class TestCooldowns:
    def test_no_cooldown_initially(self):
        assert is_provider_cooled_down("anthropic", "claude") is False

    def test_cooldown_active_after_failure(self):
        record_provider_failure("openai", "gpt-4o")
        assert is_provider_cooled_down("openai", "gpt-4o") is True

    def test_cooldown_expires(self):
        llm_config._provider_cooldowns["openai:gpt-4o"] = time.monotonic() - _COOLDOWN_SECONDS - 1
        assert is_provider_cooled_down("openai", "gpt-4o") is False

    def test_separate_models(self):
        record_provider_failure("openai", "gpt-4o")
        assert is_provider_cooled_down("openai", "gpt-4o-mini") is False


# ─── Quality routing ─────────────────────────────────────────────────────────


class TestSelectHighestQuality:
    def test_selects_tier_1(self):
        models = [
            {"provider": "openai", "model": "gpt-4o-mini"},      # tier 2
            {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},  # tier 1
            {"provider": "google", "model": "gemini-2.0-flash"},  # tier 2
        ]
        result = _select_highest_quality(models)
        assert result["model"] == "claude-sonnet-4-20250514"

    def test_unknown_model_lowest_priority(self):
        models = [
            {"provider": "openai", "model": "gpt-4o-mini"},       # tier 2
            {"provider": "custom", "model": "custom-model-v1"},   # tier 99
        ]
        result = _select_highest_quality(models)
        assert result["model"] == "gpt-4o-mini"

    def test_single_model(self):
        models = [{"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}]
        result = _select_highest_quality(models)
        assert result["model"] == "claude-haiku-4-5-20251001"


# ─── Fastest routing ─────────────────────────────────────────────────────────


class TestSelectFastest:
    @pytest.mark.asyncio
    async def test_selects_lowest_p95_latency(self):
        """p95 latency wins over avg_latency and static fallback."""
        models = [
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "google", "model": "gemini-2.0-flash"},
        ]
        live = {
            "anthropic:claude-haiku-4-5-20251001": {"p95_latency_ms": 900.0, "avg_latency_ms": 600.0},
            "openai:gpt-4o-mini": {"p95_latency_ms": 700.0, "avg_latency_ms": 500.0},
            "google:gemini-2.0-flash": {"p95_latency_ms": 500.0, "avg_latency_ms": 400.0},
        }
        with patch("benchmarks.get_model_benchmarks", new_callable=AsyncMock, return_value=live):
            result = await _select_fastest(models)
        assert result["model"] == "gemini-2.0-flash"

    @pytest.mark.asyncio
    async def test_falls_back_to_avg_when_no_p95(self):
        """When p95 is absent, avg_latency_ms is used."""
        models = [
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
            {"provider": "openai", "model": "gpt-4o-mini"},
        ]
        live = {
            "anthropic:claude-haiku-4-5-20251001": {"avg_latency_ms": 600.0},
            "openai:gpt-4o-mini": {"avg_latency_ms": 450.0},
        }
        with patch("benchmarks.get_model_benchmarks", new_callable=AsyncMock, return_value=live):
            result = await _select_fastest(models)
        assert result["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_falls_back_to_static_when_no_benchmarks(self):
        """Empty live benchmarks → static default_latency_ms from catalogue."""
        models = [
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},  # 600ms
            {"provider": "openai", "model": "gpt-4o-mini"},                    # 500ms
            {"provider": "google", "model": "gemini-2.0-flash"},               # 400ms
        ]
        with patch("benchmarks.get_model_benchmarks", new_callable=AsyncMock, return_value={}):
            result = await _select_fastest(models)
        assert result["model"] == "gemini-2.0-flash"

    @pytest.mark.asyncio
    async def test_single_model_returns_it(self):
        models = [{"provider": "openai", "model": "gpt-4o-mini"}]
        with patch("benchmarks.get_model_benchmarks", new_callable=AsyncMock, return_value={}):
            result = await _select_fastest(models)
        assert result["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_benchmark_exception_returns_first_and_logs(self):
        """Exception from get_model_benchmarks → falls back to static, logs warning."""
        models = [
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},  # 600ms static
            {"provider": "openai", "model": "gpt-4o-mini"},                    # 500ms static
        ]
        with patch(
            "benchmarks.get_model_benchmarks",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ), patch("llm_config.logger") as mock_log:
            result = await _select_fastest(models)
        # Static fallback: gpt-4o-mini (500ms) is faster than claude-haiku (600ms)
        assert result["model"] == "gpt-4o-mini"
        mock_log.warning.assert_called_once()


# ─── Cheapest routing ────────────────────────────────────────────────────────


class TestSelectCheapest:
    @pytest.mark.asyncio
    async def test_selects_cheapest_by_token_cost(self):
        models = [
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "google", "model": "gemini-2.0-flash"},
        ]

        def _make_rule(tokens_in, tokens_out):
            r = MagicMock()
            r.tokens_in_cost_per_1k = tokens_in
            r.tokens_out_cost_per_1k = tokens_out
            return r

        pricing = {
            ("anthropic", "claude-haiku-4-5-20251001"): _make_rule(0.25, 1.25),
            ("openai", "gpt-4o-mini"): _make_rule(0.15, 0.60),
            ("google", "gemini-2.0-flash"): _make_rule(0.10, 0.40),
        }

        async def mock_pricing(provider, model):
            return pricing.get((provider, model))

        with patch("billing.get_live_pricing_for_model", side_effect=mock_pricing):
            result = await _select_cheapest(models)
        assert result["model"] == "gemini-2.0-flash"

    @pytest.mark.asyncio
    async def test_no_pricing_returns_first(self):
        """When all pricing lookups return None, first model is returned."""
        models = [
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
            {"provider": "openai", "model": "gpt-4o-mini"},
        ]
        with patch(
            "billing.get_live_pricing_for_model",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await _select_cheapest(models)
        assert result["model"] == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_pricing_exception_skips_model(self):
        """A model that raises on pricing lookup is skipped; next cheapest wins."""
        models = [
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
            {"provider": "openai", "model": "gpt-4o-mini"},
        ]

        call_count = 0

        async def flaky_pricing(provider, model):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("pricing db error")
            r = MagicMock()
            r.tokens_in_cost_per_1k = 0.15
            r.tokens_out_cost_per_1k = 0.60
            return r

        with patch("billing.get_live_pricing_for_model", side_effect=flaky_pricing):
            result = await _select_cheapest(models)
        assert result["model"] == "gpt-4o-mini"


# ─── resolve_llm_config ──────────────────────────────────────────────────────


class TestResolveLlmConfig:
    @pytest.mark.asyncio
    async def test_no_config_default_returns_none(self, mock_settings):
        """No org config + default routing → None (use global)."""
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None):
            result = await resolve_llm_config("org-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_config_explicit_routing_routes_from_pool(self, mock_settings):
        """No org config + cost routing → routes from default pool."""
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None), \
             patch("llm_config._route_from_pool", new_callable=AsyncMock) as mock_route:
            mock_route.return_value = {"provider": "openai", "model": "gpt-4o-mini"}
            result = await resolve_llm_config("org-1", routing_preference="cost")
        mock_route.assert_called_once_with("cost")

    @pytest.mark.asyncio
    async def test_byok_always_uses_own_config(self, mock_settings):
        """BYOK org never smart-routes — always uses its own config."""
        with patch("llm_config.get_settings", return_value=mock_settings):
            enc = _encrypt_llm_key("my-key")

        row = _make_row(api_key_enc=enc, is_byok=True)
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=row)
        llm_config._pool = pool

        byok_cfg = OrgLlmConfig(
            org_id="org-1", provider="anthropic", model="claude-haiku-4-5-20251001",
            has_api_key=True, is_byok=True,
        )

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None), \
             patch("llm_config.get_org_llm_config_cached", new_callable=AsyncMock, return_value=byok_cfg):
            result = await resolve_llm_config("org-1", routing_preference="cost")

        # Should use build_llm_config_dict, not _route_from_pool
        assert result is not None
        assert result["api_key"] == "my-key"

    @pytest.mark.asyncio
    async def test_non_byok_with_quality_routing(self, mock_settings):
        """Non-BYOK org with quality routing → routes from pool."""
        cfg = OrgLlmConfig(
            org_id="org-1", provider="anthropic", model="claude-haiku-4-5-20251001",
            is_byok=False, routing_preference="quality",
        )

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None), \
             patch("llm_config.get_org_llm_config_cached", new_callable=AsyncMock, return_value=cfg), \
             patch("llm_config._route_from_pool", new_callable=AsyncMock) as mock_route:
            mock_route.return_value = {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}
            result = await resolve_llm_config("org-1")
        mock_route.assert_called_once_with("quality")


# ─── _route_from_pool ────────────────────────────────────────────────────────


class TestRouteFromPool:
    @pytest.mark.asyncio
    async def test_empty_pool_returns_none(self):
        settings = MagicMock(default_model_pool=[])
        with patch("llm_config.get_settings", return_value=settings):
            result = await llm_config._route_from_pool("cost")
        assert result is None

    @pytest.mark.asyncio
    async def test_default_routing_picks_first(self, mock_settings):
        with patch("llm_config.get_settings", return_value=mock_settings):
            result = await llm_config._route_from_pool("default")
        assert result["provider"] == "anthropic"
        assert result["api_key"] == "shared-anthropic-key"

    @pytest.mark.asyncio
    async def test_cooled_down_models_filtered(self, mock_settings):
        """Cooled-down models are skipped."""
        record_provider_failure("anthropic", "claude-haiku-4-5-20251001")
        with patch("llm_config.get_settings", return_value=mock_settings):
            result = await llm_config._route_from_pool("default")
        assert result["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_all_cooled_down_uses_first_anyway(self, mock_settings):
        """When all models are cooled down, use first as best-effort."""
        for m in mock_settings.default_model_pool:
            record_provider_failure(m["provider"], m["model"])
        with patch("llm_config.get_settings", return_value=mock_settings):
            result = await llm_config._route_from_pool("default")
        assert result["provider"] == "anthropic"  # first in pool


# ─── Cache invalidation ──────────────────────────────────────────────────────


class TestCacheInvalidation:
    @pytest.mark.asyncio
    async def test_invalidate_clears_in_process(self):
        llm_config._config_cache["org-1"] = (None, time.monotonic() + 999)
        with patch("llm_config.get_redis", return_value=None):
            await invalidate_llm_config_cache("org-1")
        assert "org-1" not in llm_config._config_cache

    @pytest.mark.asyncio
    async def test_invalidate_clears_redis(self):
        llm_config._config_cache["org-1"] = (None, time.monotonic() + 999)
        mock_redis = AsyncMock()
        with patch("llm_config.get_redis", return_value=mock_redis):
            await invalidate_llm_config_cache("org-1")
        mock_redis.delete.assert_called_once_with("teardrop:llm_config:org-1")

    @pytest.mark.asyncio
    async def test_invalidate_redis_failure_non_fatal(self):
        """Redis failure during invalidation is logged, not raised."""
        llm_config._config_cache["org-1"] = (None, time.monotonic() + 999)
        mock_redis = AsyncMock()
        mock_redis.delete.side_effect = Exception("Redis down")
        with patch("llm_config.get_redis", return_value=mock_redis):
            await invalidate_llm_config_cache("org-1")
        # Should not raise — in-process cache still cleared
        assert "org-1" not in llm_config._config_cache


# ─── upsert_org_llm_config ───────────────────────────────────────────────────


class TestUpsertOrgLlmConfig:
    @pytest.mark.asyncio
    async def test_upsert_with_api_key(self, mock_settings):
        pool = AsyncMock()
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None):
            cfg = await upsert_org_llm_config(
                "org-1", provider="anthropic", model="claude-haiku-4-5-20251001",
                api_key="sk-byok-key",
            )

        assert cfg.is_byok is True
        assert cfg.has_api_key is True
        pool.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_without_api_key(self, mock_settings):
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value={"is_byok": False, "has_api_key": False})
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None):
            cfg = await upsert_org_llm_config(
                "org-1", provider="openai", model="gpt-4o-mini",
            )

        assert cfg.is_byok is False
        assert cfg.has_api_key is False
        pool.fetchrow.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_preserve_existing_byok_key(self, mock_settings):
        """Omitting api_key on an existing BYOK row preserves the key and is_byok state."""
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value={"is_byok": True, "has_api_key": True})
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None):
            cfg = await upsert_org_llm_config(
                "org-1", provider="anthropic", model="claude-haiku-4-5-20251001",
            )

        assert cfg.is_byok is True
        assert cfg.has_api_key is True
        pool.fetchrow.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_clear_api_key(self, mock_settings):
        """Passing clear_api_key=True removes the BYOK key without deleting the config."""
        pool = AsyncMock()
        llm_config._pool = pool

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None):
            cfg = await upsert_org_llm_config(
                "org-1", provider="anthropic", model="claude-haiku-4-5-20251001",
                clear_api_key=True,
            )

        assert cfg.is_byok is False
        assert cfg.has_api_key is False
        pool.execute.assert_called_once()
        sql = pool.execute.call_args[0][0]
        assert "api_key_enc = NULL" in sql
        assert "is_byok = FALSE" in sql

    @pytest.mark.asyncio
    async def test_upsert_invalidates_cache(self, mock_settings):
        pool = AsyncMock()
        llm_config._pool = pool
        llm_config._config_cache["org-1"] = (None, time.monotonic() + 999)

        with patch("llm_config.get_settings", return_value=mock_settings), \
             patch("llm_config.get_redis", return_value=None):
            await upsert_org_llm_config(
                "org-1", provider="anthropic", model="claude-haiku-4-5-20251001",
            )

        assert "org-1" not in llm_config._config_cache
