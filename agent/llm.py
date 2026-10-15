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
    from langchain_openai import ChatOpenAI

    from agent._openrouter import ChatOpenRouter
except ImportError:
    ChatOpenAI = None  # type: ignore[assignment,misc]
    ChatOpenRouter = None  # type: ignore[assignment,misc]

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError:
    ChatGoogleGenerativeAI = None  # type: ignore[assignment,misc]

try:
    from langchain_anthropic import ChatAnthropic
except ImportError:
    ChatAnthropic = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# ── Shared/default providers (validated at config and request boundaries) ─────

ALLOWED_PROVIDERS = frozenset({"openai", "google", "openrouter"})
BYOK_ONLY_PROVIDERS = frozenset({"anthropic"})

# Effort is a portable hint, not a provider-independent API enum. Google settings
# are resolved against documented model capabilities; native OpenAI/Anthropic
# retain their defaults. OpenRouter receives its own reasoning.effort field.
ALLOWED_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high"})


MAX_MODEL_REASONING_EFFORTS = 50


def validate_reasoning_effort(effort: str | None) -> str | None:
    """Normalise an effort level (blank means unset); raise ``ValueError`` when it is not allowed."""
    value = (effort or "").strip().lower()
    if not value:
        return None
    if value not in ALLOWED_REASONING_EFFORTS:
        raise ValueError(f"Invalid reasoning effort '{effort}'. Allowed: {', '.join(sorted(ALLOWED_REASONING_EFFORTS))}")
    return value


def validate_model_reasoning_effort(efforts: dict[str, str]) -> dict[str, str]:
    """Validate a ``{"provider:model": effort}`` map; return it normalised."""
    if len(efforts) > MAX_MODEL_REASONING_EFFORTS:
        raise ValueError(f"At most {MAX_MODEL_REASONING_EFFORTS} per-model reasoning efforts are allowed")
    normalised: dict[str, str] = {}
    for key, effort in efforts.items():
        provider, sep, model = key.partition(":")
        if not sep or not model or provider.lower() not in ALLOWED_PROVIDERS:
            raise ValueError(
                f"Invalid reasoning effort key '{key}'. Use 'provider:model' with provider in "
                f"{', '.join(sorted(ALLOWED_PROVIDERS))}"
            )
        value = validate_reasoning_effort(effort)
        if value is None:
            raise ValueError(f"Reasoning effort for '{key}' must not be blank; omit the key instead")
        normalised[f"{provider.lower()}:{model}"] = value
    return normalised


def platform_reasoning_effort(provider: str, model: str, settings: Any | None = None) -> str | None:
    """Return the operator's per-model default effort (``model_reasoning_effort``), if any."""
    if settings is None:
        settings = get_settings()
    efforts = getattr(settings, "model_reasoning_effort", None)
    if not isinstance(efforts, dict):
        return None
    return efforts.get(f"{provider.lower()}:{model}") or None


# Explicit IDs prevent new model releases from inheriting unsupported levels.
# https://ai.google.dev/gemini-api/docs/generate-content/thinking
# https://ai.google.dev/gemini-api/docs/gemini-3
_GOOGLE_THINKING_LEVELS = {
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-4-argon": ("low", "medium", "high"),
    "gemini-3.1-pro-preview": ("low", "medium", "high"),
    "gemini-3.6-flash": ("minimal", "low", "medium", "high"),
    "gemini-3-flash-preview": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3.1-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3.1-flash-lite-image": ("minimal", "high"),
}
_GOOGLE_MIN_THINKING_BUDGET = {
    "gemini-2.5-pro": 128,
    "gemini-2.5-flash": 0,
    "gemini-2.5-flash-lite": 0,
}
_THINKING_LEVEL_ORDER = ("minimal", "low", "medium", "high")


def _google_thinking_kwargs(model: str, effort: str | None) -> dict[str, Any]:
    """Translate effort only when the model's native capabilities are known.

    ``none`` means the lowest available effort, not guaranteed thinking-off.
    Budget-only models have no exact equivalent for the other effort labels;
    unknown models likewise retain provider defaults rather than guessed settings.
    """
    effort = validate_reasoning_effort(effort)
    if effort is None:
        return {}
    model = model.removeprefix("models/")
    levels = _GOOGLE_THINKING_LEVELS.get(model)
    if levels is not None:
        requested = "minimal" if effort == "none" else effort
        for level in _THINKING_LEVEL_ORDER[_THINKING_LEVEL_ORDER.index(requested) :]:
            if level in levels:
                return {"thinking_level": level}
    if effort == "none" and model in _GOOGLE_MIN_THINKING_BUDGET:
        return {"thinking_budget": _GOOGLE_MIN_THINKING_BUDGET[model]}
    return {}


# ─── Global singleton (backward compat) ──────────────────────────────────────

_llm: BaseChatModel | None = None


def create_llm(settings: Any | None = None) -> BaseChatModel:
    """Construct a ``BaseChatModel`` based on the configured provider.

    Supported providers:
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
    reasoning_effort = platform_reasoning_effort(provider, settings.agent_model, settings)

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
        google_kwargs: dict[str, Any] = {**common, "google_api_key": settings.google_api_key or None}
        google_kwargs.update(_google_thinking_kwargs(settings.agent_model, reasoning_effort))
        return ChatGoogleGenerativeAI(**google_kwargs)  # type: ignore[arg-type]

    if provider == "openrouter":
        if ChatOpenAI is None:
            raise RuntimeError("langchain-openai is not installed. Run: pip install langchain-openai")
        kwargs: dict[str, Any] = {
            **common,
            "api_key": settings.openrouter_api_key or None,
            "base_url": "https://openrouter.ai/api/v1",
        }
        if reasoning_effort:
            kwargs["extra_body"] = {"reasoning": {"effort": reasoning_effort}}
        return ChatOpenRouter(**kwargs)  # type: ignore[arg-type]

    raise ValueError(f"Unknown agent_provider '{provider}'. Supported: openai, google, openrouter.")


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
        provider        — "openai" | "google" | "openrouter"; "anthropic" for BYOK only
        model           — model identifier string
        api_key         — provider API key (required)
        is_byok         — true when the API key belongs to the org
        api_base         — optional custom base URL (OpenAI-compatible endpoints)
        max_tokens      — int (default 4096)
        temperature     — float (default 0.0)
        timeout_seconds — int (default 120)
        reasoning_effort — optional effort hint (e.g. "low") for reasoning models.
            On reasoning models the ``max_tokens`` budget is shared between hidden
            reasoning tokens and visible output, so an unbounded reasoning effort can
            starve or truncate the visible response. Google translates the hint
            using documented per-model levels or budgets; unknown models retain
            defaults. OpenRouter receives ``reasoning.effort``. Native OpenAI and
            Anthropic retain defaults. When unset, the operator's per-model
            default (``model_reasoning_effort``) applies.
    """
    provider = config["provider"].lower()
    is_byok = config.get("is_byok") is True
    api_key = config.get("api_key") or ""
    if provider not in ALLOWED_PROVIDERS and not (is_byok and provider in BYOK_ONLY_PROVIDERS):
        raise ValueError(f"Unknown provider '{provider}'. Supported: {', '.join(sorted(ALLOWED_PROVIDERS))}.")

    if provider in BYOK_ONLY_PROVIDERS and not api_key:
        raise ValueError(f"Provider '{provider}' requires a non-empty BYOK API key.")

    model = config["model"]
    api_base = config.get("api_base")
    reasoning_effort = str(config.get("reasoning_effort") or platform_reasoning_effort(provider, model) or "").strip().lower()

    common: dict[str, Any] = {
        "model": model,
        "max_tokens": config.get("max_tokens", 4096),
        "temperature": config.get("temperature", 0.0),
    }

    if provider == "openai":
        if ChatOpenAI is None:
            raise RuntimeError("langchain-openai is not installed. Run: pip install langchain-openai")
        kwargs: dict[str, Any] = {**common, "api_key": api_key or None}
        if api_base:
            kwargs["base_url"] = api_base
        return ChatOpenAI(**kwargs)  # type: ignore[arg-type]

    if provider == "google":
        if ChatGoogleGenerativeAI is None:
            raise RuntimeError("langchain-google-genai is not installed. Run: pip install langchain-google-genai")
        kwargs = {**common, "google_api_key": api_key or None}
        kwargs.update(_google_thinking_kwargs(model, reasoning_effort))
        return ChatGoogleGenerativeAI(**kwargs)  # type: ignore[arg-type]

    if provider == "anthropic":
        if ChatAnthropic is None:
            raise RuntimeError("langchain-anthropic is not installed. Run: pip install langchain-anthropic")
        return ChatAnthropic(
            **common,
            api_key=api_key,
            base_url="https://api.anthropic.com",
            timeout=config.get("timeout_seconds", 120),
        )  # type: ignore[arg-type]

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
        return ChatOpenRouter(**kwargs)  # type: ignore[arg-type]

    # Should be unreachable due to ALLOWED_PROVIDERS check above.
    raise ValueError(f"Unknown provider '{provider}'.")
