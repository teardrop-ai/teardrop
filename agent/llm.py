# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Multi-provider LLM factory for Teardrop.

Provides:
- A global singleton ``BaseChatModel`` via ``get_llm()`` (backward compat).
- Per-request LLM via ``get_llm_for_request(config)`` for multi-org routing.
- ``create_llm_from_config(config)`` for explicit provider/model/key combos.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent._llm_cache import _cache_key, clear_llm_cache, get_llm_for_request  # noqa: F401
from agent._llm_usage import extract_usage  # noqa: F401
from teardrop.config import get_settings

# ── Optional provider imports — None when package not installed ───────────────
try:
    from langchain_anthropic import ChatAnthropic
except ImportError:
    ChatAnthropic = None  # type: ignore[assignment,misc]

try:
    from langchain_openai import ChatOpenAI
except ImportError:
    ChatOpenAI = None  # type: ignore[assignment,misc]

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError:
    ChatGoogleGenerativeAI = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# ── Allowed providers (validated at config and request boundaries) ────────────

ALLOWED_PROVIDERS = frozenset({"anthropic", "openai", "google", "openrouter"})

# ─── Global singleton (backward compat) ──────────────────────────────────────

_llm: BaseChatModel | None = None


def create_llm(settings: Any | None = None) -> BaseChatModel:
    """Construct a ``BaseChatModel`` based on the configured provider.

    Supported providers:
    - ``anthropic``   — ``langchain-anthropic`` (``ChatAnthropic``)
    - ``openai``      — ``langchain-openai`` (``ChatOpenAI``)
    - ``google``      — ``langchain-google-genai`` (``ChatGoogleGenerativeAI``)
    - ``openrouter``  — ``langchain-openai`` via OpenRouter proxy (``ChatOpenAI``)
    """
    if settings is None:
        settings = get_settings()

    provider = settings.agent_provider.lower()
    common: dict[str, Any] = {
        "model": settings.agent_model,
        "max_tokens": settings.agent_max_tokens,
        "temperature": settings.agent_temperature,
    }

    if provider == "anthropic":
        if ChatAnthropic is None:
            raise RuntimeError("langchain-anthropic is not installed. Run: pip install langchain-anthropic")
        return ChatAnthropic(
            **common,
            api_key=settings.anthropic_api_key or None,  # type: ignore[arg-type]
        )

    if provider == "openai":
        if ChatOpenAI is None:
            raise RuntimeError("langchain-openai is not installed. Run: pip install langchain-openai")
        return ChatOpenAI(
            **common,
            api_key=settings.openai_api_key or None,  # type: ignore[arg-type]
        )

    if provider == "google":
        if ChatGoogleGenerativeAI is None:
            raise RuntimeError("langchain-google-genai is not installed. Run: pip install langchain-google-genai")
        return ChatGoogleGenerativeAI(
            **common,
            google_api_key=settings.google_api_key or None,  # type: ignore[arg-type]
        )

    if provider == "openrouter":
        if ChatOpenAI is None:
            raise RuntimeError("langchain-openai is not installed. Run: pip install langchain-openai")
        kwargs: dict[str, Any] = {
            **common,
            "api_key": settings.openrouter_api_key or None,
            "base_url": "https://openrouter.ai/api/v1",
        }
        return ChatOpenAI(**kwargs)  # type: ignore[arg-type]

    raise ValueError(f"Unknown agent_provider '{provider}'. Supported: anthropic, openai, google, openrouter.")


def get_llm() -> BaseChatModel:
    """Return the module-level LLM singleton, creating it on first call."""
    global _llm
    if _llm is None:
        _llm = create_llm()
        _s = get_settings()
        logger.info("LLM initialised: provider=%s model=%s", _s.agent_provider, _s.agent_model)
    return _llm


def reset_llm() -> None:
    """Clear the cached LLM singleton (used by tests)."""
    global _llm
    _llm = None


def create_llm_from_config(config: dict[str, Any]) -> BaseChatModel:
    """Construct a ``BaseChatModel`` from an explicit config dict.

    Expected keys:
        provider        — "anthropic" | "openai" | "google" | "openrouter"
        model           — model identifier string
        api_key         — provider API key (required)
        api_base         — optional custom base URL (OpenAI-compatible endpoints)
        max_tokens      — int (default 4096)
        temperature     — float (default 0.0)
        timeout_seconds — int (default 120)
        reasoning_effort — optional effort hint (e.g. "low") for reasoning models.
            On reasoning models the ``max_tokens`` budget is shared between hidden
            reasoning tokens and visible output, so an unbounded reasoning effort can
            starve or truncate the visible response. Applied as ``thinking_level``
            (google) or ``reasoning.effort`` (openrouter). Ignored for
            anthropic/openai, where reasoning is opt-in.
    """
    provider = config["provider"].lower()
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Supported: {', '.join(sorted(ALLOWED_PROVIDERS))}.")

    api_key = config.get("api_key") or ""
    model = config["model"]
    api_base = config.get("api_base")
    reasoning_effort = str(config.get("reasoning_effort") or "").strip().lower()

    common: dict[str, Any] = {
        "model": model,
        "max_tokens": config.get("max_tokens", 4096),
        "temperature": config.get("temperature", 0.0),
    }

    if provider == "anthropic":
        if ChatAnthropic is None:
            raise RuntimeError("langchain-anthropic is not installed. Run: pip install langchain-anthropic")
        kwargs: dict[str, Any] = {**common, "api_key": api_key or None}
        if api_base:
            kwargs["base_url"] = api_base
        return ChatAnthropic(**kwargs)  # type: ignore[arg-type]

    if provider == "openai":
        if ChatOpenAI is None:
            raise RuntimeError("langchain-openai is not installed. Run: pip install langchain-openai")
        kwargs = {**common, "api_key": api_key or None}
        if api_base:
            kwargs["base_url"] = api_base
        return ChatOpenAI(**kwargs)  # type: ignore[arg-type]

    if provider == "google":
        if ChatGoogleGenerativeAI is None:
            raise RuntimeError("langchain-google-genai is not installed. Run: pip install langchain-google-genai")
        kwargs = {**common, "google_api_key": api_key or None}
        if reasoning_effort:
            kwargs["thinking_level"] = "minimal" if reasoning_effort == "none" else reasoning_effort
        return ChatGoogleGenerativeAI(**kwargs)  # type: ignore[arg-type]

    if provider == "openrouter":
        # OpenRouter exposes an OpenAI-compatible API at a fixed base URL.
        # Provider eligibility is delegated to OpenRouter so API-key data policies
        # such as ZDR can determine the eligible inference pool.
        if ChatOpenAI is None:
            raise RuntimeError("langchain-openai is not installed. Run: pip install langchain-openai")
        kwargs = {
            **common,
            "api_key": api_key or None,
            "base_url": api_base or "https://openrouter.ai/api/v1",
        }
        extra_body: dict[str, Any] = {}
        if reasoning_effort:
            extra_body["reasoning"] = {"effort": reasoning_effort}
        if extra_body:
            kwargs["extra_body"] = extra_body
        return ChatOpenAI(**kwargs)  # type: ignore[arg-type]

    # Should be unreachable due to ALLOWED_PROVIDERS check above.
    raise ValueError(f"Unknown provider '{provider}'.")
