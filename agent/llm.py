# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Multi-provider LLM factory for Teardrop.

Provides:
- A global singleton ``BaseChatModel`` via ``get_llm()`` (backward compat).
- Per-request LLM via ``get_llm_for_request(config)`` for multi-org routing.
- ``create_llm_from_config(config)`` for explicit provider/model/key combos.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from config import get_settings

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
        if settings.agent_model.startswith("deepseek/"):
            kwargs["extra_body"] = {"provider": {"only": ["NovitaAI", "DeepInfra"]}}
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


# ─── Per-request LLM construction + cache ────────────────────────────────────

_LLM_CACHE_MAX = 64
_llm_cache: OrderedDict[str, BaseChatModel] = OrderedDict()
_llm_cache_lock = threading.Lock()


def _cache_key(provider: str, model: str, api_key: str) -> str:
    """Build a cache key from provider+model+key hash.  Never stores raw keys."""
    raw = f"{provider}:{model}:{api_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


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
    """
    provider = config["provider"].lower()
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Supported: {', '.join(sorted(ALLOWED_PROVIDERS))}.")

    api_key = config.get("api_key") or ""
    model = config["model"]
    api_base = config.get("api_base")

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
        return ChatGoogleGenerativeAI(**kwargs)  # type: ignore[arg-type]

    if provider == "openrouter":
        # OpenRouter exposes an OpenAI-compatible API at a fixed base URL.
        # DeepSeek models are pinned to US-hosted providers (NovitaAI primary, DeepInfra
        # fallback) via OpenRouter's provider-routing preference to avoid Chinese-hosted
        # inference. NovitaAI runs fp8 with a 393K output limit; DeepInfra runs fp4
        # with a 16.4K output limit and is kept as a fallback only.
        if ChatOpenAI is None:
            raise RuntimeError("langchain-openai is not installed. Run: pip install langchain-openai")
        kwargs = {
            **common,
            "api_key": api_key or None,
            "base_url": api_base or "https://openrouter.ai/api/v1",
        }
        if model.startswith("deepseek/"):
            kwargs["extra_body"] = {"provider": {"only": ["NovitaAI", "DeepInfra"]}}
        return ChatOpenAI(**kwargs)  # type: ignore[arg-type]

    # Should be unreachable due to ALLOWED_PROVIDERS check above.
    raise ValueError(f"Unknown provider '{provider}'.")


def get_llm_for_request(llm_config: dict[str, Any] | None = None) -> BaseChatModel:
    """Resolve an LLM for a single agent run.

    If *llm_config* is ``None``, falls back to the global singleton (backward
    compatible — existing orgs without config are unaffected).

    Identical configs (same provider+model+key) share a cached instance to
    avoid re-creating HTTP clients on every request.
    """
    if llm_config is None:
        return get_llm()

    key = _cache_key(
        llm_config["provider"],
        llm_config["model"],
        llm_config.get("api_key", ""),
    )

    with _llm_cache_lock:
        if key in _llm_cache:
            _llm_cache.move_to_end(key)
            return _llm_cache[key]

    # Build outside the lock to avoid blocking other coroutines.
    llm = create_llm_from_config(llm_config)
    logger.info(
        "LLM created for request: provider=%s model=%s",
        llm_config["provider"],
        llm_config["model"],
    )

    with _llm_cache_lock:
        _llm_cache[key] = llm
        _llm_cache.move_to_end(key)
        while len(_llm_cache) > _LLM_CACHE_MAX:
            _llm_cache.popitem(last=False)

    return llm


def clear_llm_cache() -> None:
    """Purge the per-request LLM cache (used by tests)."""
    with _llm_cache_lock:
        _llm_cache.clear()


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
