# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Multi-provider LLM factory for Teardrop.

Provides a singleton ``BaseChatModel`` instance selected by
``settings.agent_provider`` (anthropic | openai | google).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from config import get_settings

logger = logging.getLogger(__name__)

# ─── Singleton ────────────────────────────────────────────────────────────────

_llm: BaseChatModel | None = None


def create_llm(settings: Any | None = None) -> BaseChatModel:
    """Construct a ``BaseChatModel`` based on the configured provider.

    Supported providers:
    - ``anthropic`` — ``langchain-anthropic`` (``ChatAnthropic``)
    - ``openai``    — ``langchain-openai`` (``ChatOpenAI``)
    - ``google``    — ``langchain-google-genai`` (``ChatGoogleGenerativeAI``)
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
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            **common,
            api_key=settings.anthropic_api_key or None,  # type: ignore[arg-type]
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            **common,
            api_key=settings.openai_api_key or None,  # type: ignore[arg-type]
        )

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            **common,
            google_api_key=settings.google_api_key or None,  # type: ignore[arg-type]
        )

    raise ValueError(
        f"Unknown agent_provider '{provider}'. "
        "Supported: anthropic, openai, google."
    )


def get_llm() -> BaseChatModel:
    """Return the module-level LLM singleton, creating it on first call."""
    global _llm
    if _llm is None:
        _llm = create_llm()
        logger.info("LLM initialised: provider=%s model=%s", get_settings().agent_provider, get_settings().agent_model)
    return _llm


def reset_llm() -> None:
    """Clear the cached LLM singleton (used by tests)."""
    global _llm
    _llm = None


# ─── Usage normalisation ─────────────────────────────────────────────────────


def extract_usage(response: AIMessage) -> dict[str, int]:
    """Extract ``tokens_in`` / ``tokens_out`` from an LLM response.

    Different providers use different key names in ``usage_metadata``:
    - Anthropic: ``input_tokens``, ``output_tokens``
    - OpenAI:    ``input_tokens``, ``output_tokens`` (LangChain normalises)
    - Google:    ``input_tokens``, ``output_tokens`` (LangChain normalises)

    LangChain ≥ 0.1.45 normalises most providers to ``input_tokens`` /
    ``output_tokens``, so this helper is forward-compatible. It also handles
    the legacy OpenAI ``prompt_tokens`` / ``completion_tokens`` keys as a
    fallback.
    """
    if not hasattr(response, "usage_metadata") or not response.usage_metadata:
        return {"tokens_in": 0, "tokens_out": 0}

    meta = response.usage_metadata

    tokens_in = meta.get("input_tokens") or meta.get("prompt_tokens") or 0
    tokens_out = meta.get("output_tokens") or meta.get("completion_tokens") or 0

    return {"tokens_in": int(tokens_in), "tokens_out": int(tokens_out)}
