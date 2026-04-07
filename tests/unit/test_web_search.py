"""Unit tests for tools/definitions/web_search.py.

No real HTTP calls are made; Tavily client and config are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from tools.definitions.web_search import WebSearchInput, _stub_results, web_search

# ─── Input schema validation ──────────────────────────────────────────────────


class TestWebSearchInput:
    def test_valid_input(self):
        inp = WebSearchInput(query="test query", num_results=5)
        assert inp.query == "test query"
        assert inp.num_results == 5

    def test_num_results_min_boundary(self):
        inp = WebSearchInput(query="q", num_results=1)
        assert inp.num_results == 1

    def test_num_results_max_boundary(self):
        inp = WebSearchInput(query="q", num_results=20)
        assert inp.num_results == 20

    def test_num_results_below_min_raises(self):
        with pytest.raises(ValidationError):
            WebSearchInput(query="q", num_results=0)

    def test_num_results_above_max_raises(self):
        with pytest.raises(ValidationError):
            WebSearchInput(query="q", num_results=21)

    def test_query_max_length_enforced(self):
        with pytest.raises(ValidationError):
            WebSearchInput(query="x" * 501, num_results=5)


# ─── Stub fallback ────────────────────────────────────────────────────────────


class TestStubResults:
    def test_stub_returns_correct_structure(self):
        result = _stub_results("climate change", 3)
        assert result["query"] == "climate change"
        assert result["num_results"] == 3
        # Stub always returns a single placeholder entry regardless of num_results
        assert len(result["results"]) == 1
        assert result["results"][0]["url"] == "https://example.com"

    def test_stub_results_have_required_fields(self):
        result = _stub_results("test", 1)
        r = result["results"][0]
        assert "title" in r
        assert "url" in r
        assert "snippet" in r

    def test_stub_returns_requested_count(self):
        # The stub always returns a fixed single placeholder entry.
        # num_results is echoed in the metadata but doesn't multiply results.
        for count in [1, 5, 10, 20]:
            result = _stub_results("query", count)
            assert result["num_results"] == count
            assert len(result["results"]) >= 1


# ─── web_search dispatcher ───────────────────────────────────────────────────


class TestWebSearch:
    async def test_uses_stub_when_no_api_key(self, test_settings, monkeypatch):
        """When TAVILY_API_KEY is absent, stub results are returned.

        test_settings does not set TAVILY_API_KEY so Settings.tavily_api_key is None.
        """
        result = await web_search("Python asyncio", num_results=3)

        assert result["query"] == "Python asyncio"
        assert len(result["results"]) >= 1

    async def test_uses_tavily_when_api_key_present(self, test_settings, monkeypatch):
        """When TAVILY_API_KEY is set, _tavily_search is called."""
        import config as _config

        mock_tavily_result = {
            "query": "OpenAI",
            "num_results": 2,
            "results": [
                {
                    "title": "OpenAI.com",
                    "url": "https://openai.com",
                    "snippet": "AI lab",
                    "score": 0.9,
                },
                {
                    "title": "GPT-4",
                    "url": "https://openai.com/gpt4",
                    "snippet": "Model",
                    "score": 0.8,
                },
            ],
        }
        monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
        _config.get_settings.cache_clear()

        monkeypatch.setattr(
            "tools.definitions.web_search._tavily_search",
            AsyncMock(return_value=mock_tavily_result),
        )

        result = await web_search("OpenAI", num_results=2)

        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "OpenAI.com"

    async def test_tavily_exception_propagates(self, test_settings, monkeypatch):
        """If Tavily raises, the exception is not silently swallowed by web_search."""
        import config as _config

        monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
        _config.get_settings.cache_clear()

        monkeypatch.setattr(
            "tools.definitions.web_search._tavily_search",
            AsyncMock(side_effect=Exception("Tavily API error")),
        )

        with pytest.raises(Exception, match="Tavily API error"):
            await web_search("test", num_results=1)
