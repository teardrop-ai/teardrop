"""Unit tests for agent/llm.py — multi-provider LLM factory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from agent.llm import create_llm, extract_usage, reset_llm


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
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


# ─── create_llm dispatch ─────────────────────────────────────────────────────


class TestCreateLlm:
    def test_anthropic_provider(self):
        settings = _make_settings(agent_provider="anthropic")
        with patch("agent.llm.ChatAnthropic", autospec=True) as MockCls:
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
        with patch("agent.llm.ChatOpenAI", autospec=True) as MockCls:
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
        with patch("agent.llm.ChatGoogleGenerativeAI", autospec=True) as MockCls:
            llm = create_llm(settings)
            MockCls.assert_called_once_with(
                model="gemini-2.0-flash",
                max_tokens=4096,
                temperature=0.0,
                google_api_key="test-google-key",
            )
            assert llm is MockCls.return_value

    def test_unknown_provider_raises_value_error(self):
        settings = _make_settings(agent_provider="cohere")
        with pytest.raises(ValueError, match="Unknown agent_provider 'cohere'"):
            create_llm(settings)

    def test_provider_is_case_insensitive(self):
        settings = _make_settings(agent_provider="ANTHROPIC")
        with patch("agent.llm.ChatAnthropic", autospec=True) as MockCls:
            create_llm(settings)
            MockCls.assert_called_once()

    def test_empty_api_key_passes_none(self):
        settings = _make_settings(agent_provider="anthropic", anthropic_api_key="")
        with patch("agent.llm.ChatAnthropic", autospec=True) as MockCls:
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
            mock_create.return_value = MagicMock()
            a = get_llm()
            reset_llm()
            b = get_llm()
            assert a is not b
            assert mock_create.call_count == 2
