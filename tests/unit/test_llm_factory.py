"""Unit tests for agent/llm.py — multi-provider LLM factory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from agent.llm import (
    ALLOWED_PROVIDERS,
    _cache_key,
    clear_llm_cache,
    create_llm,
    create_llm_from_config,
    extract_usage,
    get_llm_for_request,
    reset_llm,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_settings(**overrides):
    defaults = dict(
        agent_provider="anthropic",
        agent_model="claude-haiku-4-5-20251001",
        agent_max_tokens=4096,
        agent_temperature=0.0,
        anthropic_api_key="test-anthropic-key",
        openai_api_key="test-openai-key",
        google_api_key="test-google-key",
        openrouter_api_key="test-openrouter-key",
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


# ─── create_llm dispatch ─────────────────────────────────────────────────────


class TestCreateLlm:
    def test_anthropic_provider(self):
        settings = _make_settings(agent_provider="anthropic")
        with patch("agent.llm.ChatAnthropic") as MockCls:
            llm = create_llm(settings)
            MockCls.assert_called_once_with(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                temperature=0.0,
                api_key="test-anthropic-key",
            )
            assert llm is MockCls.return_value

    def test_openai_provider(self):
        settings = _make_settings(agent_provider="openai", agent_model="gpt-4o-mini")
        with patch("agent.llm.ChatOpenAI") as MockCls:
            llm = create_llm(settings)
            MockCls.assert_called_once_with(
                model="gpt-4o-mini",
                max_tokens=4096,
                temperature=0.0,
                api_key="test-openai-key",
            )
            assert llm is MockCls.return_value

    def test_google_provider(self):
        settings = _make_settings(agent_provider="google", agent_model="gemini-2.0-flash")
        with patch("agent.llm.ChatGoogleGenerativeAI") as MockCls:
            llm = create_llm(settings)
            MockCls.assert_called_once_with(
                model="gemini-2.0-flash",
                max_tokens=4096,
                temperature=0.0,
                google_api_key="test-google-key",
            )
            assert llm is MockCls.return_value

    def test_openrouter_provider(self):
        settings = _make_settings(
            agent_provider="openrouter",
            agent_model="mistral/mistral-7b-instruct",
            openrouter_api_key="test-openrouter-key",
        )
        with patch("agent.llm.ChatOpenAI") as MockCls:
            llm = create_llm(settings)
            call_kwargs = MockCls.call_args[1]
            assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"
            assert call_kwargs["api_key"] == "test-openrouter-key"
            assert "model_kwargs" not in call_kwargs
            assert llm is MockCls.return_value

    def test_openrouter_deepseek_model_gets_provider_routing(self):
        settings = _make_settings(
            agent_provider="openrouter",
            agent_model="deepseek/deepseek-v3.2",
            openrouter_api_key="test-openrouter-key",
        )
        with patch("agent.llm.ChatOpenAI") as MockCls:
            create_llm(settings)
            call_kwargs = MockCls.call_args[1]
            assert call_kwargs["model_kwargs"] == {
                "extra_body": {"provider": {"only": ["DeepInfra"]}}
            }

    def test_unknown_provider_raises_value_error(self):
        settings = _make_settings(agent_provider="cohere")
        with pytest.raises(ValueError, match="Unknown agent_provider 'cohere'"):
            create_llm(settings)

    def test_provider_is_case_insensitive(self):
        settings = _make_settings(agent_provider="ANTHROPIC")
        with patch("agent.llm.ChatAnthropic") as MockCls:
            create_llm(settings)
            MockCls.assert_called_once()

    def test_empty_api_key_passes_none(self):
        settings = _make_settings(agent_provider="anthropic", anthropic_api_key="")
        with patch("agent.llm.ChatAnthropic") as MockCls:
            create_llm(settings)
            MockCls.assert_called_once_with(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                temperature=0.0,
                api_key=None,
            )


# ─── extract_usage normalisation ─────────────────────────────────────────────


class TestExtractUsage:
    def test_anthropic_format(self):
        msg = MagicMock(spec=AIMessage)
        msg.usage_metadata = {"input_tokens": 100, "output_tokens": 50}
        result = extract_usage(msg)
        assert result == {"tokens_in": 100, "tokens_out": 50}

    def test_openai_legacy_format(self):
        """LangChain older versions may pass prompt_tokens/completion_tokens."""
        msg = MagicMock(spec=AIMessage)
        msg.usage_metadata = {"prompt_tokens": 200, "completion_tokens": 75}
        result = extract_usage(msg)
        assert result == {"tokens_in": 200, "tokens_out": 75}

    def test_normalised_format_preferred(self):
        """When both formats present, input_tokens/output_tokens wins."""
        msg = MagicMock(spec=AIMessage)
        msg.usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 50,
            "prompt_tokens": 999,
            "completion_tokens": 999,
        }
        result = extract_usage(msg)
        assert result == {"tokens_in": 100, "tokens_out": 50}

    def test_missing_usage_metadata(self):
        msg = MagicMock(spec=AIMessage)
        msg.usage_metadata = None
        result = extract_usage(msg)
        assert result == {"tokens_in": 0, "tokens_out": 0}

    def test_no_usage_metadata_attr(self):
        msg = MagicMock(spec=AIMessage, spec_set=False)
        del msg.usage_metadata  # Simulate missing attribute entirely
        result = extract_usage(msg)
        assert result == {"tokens_in": 0, "tokens_out": 0}

    def test_empty_usage_metadata(self):
        msg = MagicMock(spec=AIMessage)
        msg.usage_metadata = {}
        result = extract_usage(msg)
        assert result == {"tokens_in": 0, "tokens_out": 0}


# ─── Singleton (get_llm / reset_llm) ─────────────────────────────────────────


class TestSingleton:
    def setup_method(self):
        reset_llm()

    def teardown_method(self):
        reset_llm()

    def test_get_llm_returns_same_instance(self):
        from agent.llm import get_llm

        with patch("agent.llm.create_llm") as mock_create:
            mock_create.return_value = MagicMock()
            a = get_llm()
            b = get_llm()
            assert a is b
            mock_create.assert_called_once()

    def test_reset_llm_clears_cache(self):
        from agent.llm import get_llm

        with patch("agent.llm.create_llm") as mock_create:
            mock_create.side_effect = lambda: MagicMock()
            a = get_llm()
            reset_llm()
            b = get_llm()
            assert a is not b
            assert mock_create.call_count == 2


# ─── Per-request LLM (Phase 2) ───────────────────────────────────────────────


def _make_config(**overrides) -> dict:
    defaults = {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "api_key": "sk-test-key-12345",
        "api_base": None,
        "max_tokens": 4096,
        "temperature": 0.0,
        "timeout_seconds": 120,
    }
    defaults.update(overrides)
    return defaults


class TestAllowedProviders:
    def test_contains_expected(self):
        assert "anthropic" in ALLOWED_PROVIDERS
        assert "openai" in ALLOWED_PROVIDERS
        assert "google" in ALLOWED_PROVIDERS
        assert "openrouter" in ALLOWED_PROVIDERS

    def test_rejects_unknown(self):
        assert "mistral" not in ALLOWED_PROVIDERS


class TestCreateLlmFromConfig:
    @patch("agent.llm.ChatAnthropic")
    def test_anthropic(self, mock_cls):
        mock_cls.return_value = MagicMock()
        config = _make_config(provider="anthropic")
        result = create_llm_from_config(config)
        assert result is mock_cls.return_value
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
        assert call_kwargs["max_tokens"] == 4096

    @patch("agent.llm.ChatOpenAI")
    def test_openai(self, mock_cls):
        mock_cls.return_value = MagicMock()
        config = _make_config(provider="openai", model="gpt-4o-mini")
        result = create_llm_from_config(config)
        assert result is mock_cls.return_value

    @patch("agent.llm.ChatOpenAI")
    def test_openai_with_base_url(self, mock_cls):
        mock_cls.return_value = MagicMock()
        config = _make_config(
            provider="openai",
            model="llama3",
            api_base="http://gpu.example.com:8000/v1",
        )
        create_llm_from_config(config)
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["base_url"] == "http://gpu.example.com:8000/v1"

    @patch("agent.llm.ChatGoogleGenerativeAI")
    def test_google(self, mock_cls):
        mock_cls.return_value = MagicMock()
        config = _make_config(provider="google", model="gemini-2.0-flash")
        result = create_llm_from_config(config)
        assert result is mock_cls.return_value

    def test_invalid_provider_raises(self):
        config = _make_config(provider="mistral")
        with pytest.raises(ValueError, match="Unknown provider"):
            create_llm_from_config(config)

    @patch("agent.llm.ChatAnthropic")
    def test_anthropic_with_base_url(self, mock_cls):
        mock_cls.return_value = MagicMock()
        config = _make_config(
            provider="anthropic",
            api_base="https://custom.anthropic.proxy.com/v1",
        )
        create_llm_from_config(config)
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["base_url"] == "https://custom.anthropic.proxy.com/v1"

    @patch("agent.llm.ChatOpenAI")
    def test_openrouter(self, mock_cls):
        mock_cls.return_value = MagicMock()
        config = _make_config(
            provider="openrouter",
            model="mistralai/mistral-7b-instruct",
            api_key="sk-or-test",
            api_base=None,
        )
        create_llm_from_config(config)
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"
        assert "model_kwargs" not in call_kwargs

    @patch("agent.llm.ChatOpenAI")
    def test_openrouter_with_deepinfra_pinning(self, mock_cls):
        """DeepSeek models must include provider routing to pin to DeepInfra."""
        mock_cls.return_value = MagicMock()
        config = _make_config(
            provider="openrouter",
            model="deepseek/deepseek-v3.2",
            api_key="sk-or-test",
            api_base=None,
        )
        create_llm_from_config(config)
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"
        assert call_kwargs["model_kwargs"] == {
            "extra_body": {"provider": {"only": ["DeepInfra"]}}
        }

    @patch("agent.llm.ChatOpenAI")
    def test_openrouter_custom_base_url(self, mock_cls):
        """An explicit api_base overrides the OpenRouter default."""
        mock_cls.return_value = MagicMock()
        config = _make_config(
            provider="openrouter",
            model="openai/gpt-4o",
            api_key="sk-or-test",
            api_base="https://custom.proxy.example.com/v1",
        )
        create_llm_from_config(config)
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["base_url"] == "https://custom.proxy.example.com/v1"


class TestCacheKey:
    def test_deterministic(self):
        k1 = _cache_key("anthropic", "claude-haiku-4-5-20251001", "sk-123")
        k2 = _cache_key("anthropic", "claude-haiku-4-5-20251001", "sk-123")
        assert k1 == k2

    def test_different_keys_differ(self):
        k1 = _cache_key("anthropic", "claude-haiku-4-5-20251001", "sk-123")
        k2 = _cache_key("anthropic", "claude-haiku-4-5-20251001", "sk-456")
        assert k1 != k2

    def test_never_contains_raw_key(self):
        key = _cache_key("anthropic", "model", "super-secret-api-key")
        assert "super-secret-api-key" not in key

    def test_is_16_chars(self):
        key = _cache_key("a", "b", "c")
        assert len(key) == 16


class TestGetLlmForRequest:
    def setup_method(self):
        clear_llm_cache()

    def teardown_method(self):
        clear_llm_cache()

    @patch("agent.llm.get_llm")
    def test_none_config_falls_back_to_singleton(self, mock_get_llm):
        mock_get_llm.return_value = MagicMock()
        result = get_llm_for_request(None)
        assert result is mock_get_llm.return_value

    @patch("agent.llm.create_llm_from_config")
    def test_config_creates_new_llm(self, mock_create):
        mock_create.return_value = MagicMock()
        config = _make_config()
        result = get_llm_for_request(config)
        assert result is mock_create.return_value

    @patch("agent.llm.create_llm_from_config")
    def test_identical_configs_cached(self, mock_create):
        mock_llm = MagicMock()
        mock_create.return_value = mock_llm
        config = _make_config()

        r1 = get_llm_for_request(config)
        r2 = get_llm_for_request(config)

        assert r1 is r2
        assert mock_create.call_count == 1

    @patch("agent.llm.create_llm_from_config")
    def test_different_configs_not_cached(self, mock_create):
        mock_create.side_effect = [MagicMock(), MagicMock()]
        c1 = _make_config(provider="anthropic")
        c2 = _make_config(provider="openai", model="gpt-4o-mini", api_key="sk-other")

        r1 = get_llm_for_request(c1)
        r2 = get_llm_for_request(c2)

        assert r1 is not r2
        assert mock_create.call_count == 2
