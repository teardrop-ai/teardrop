# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-request LLM cache and cache-key helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)

_LLM_CACHE_MAX = 64
_llm_cache: OrderedDict[str, BaseChatModel] = OrderedDict()
_llm_cache_lock = threading.Lock()


def _cache_key(
    provider: str,
    model: str,
    api_key: str,
    *,
    api_base: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    reasoning_effort: str | None = None,
) -> str:
    """Build a cache key from provider+model+key hash. Never stores raw keys."""
    raw = json.dumps(
        {
            "provider": provider.lower(),
            "model": model,
            "api_key": api_key,
            "api_base": api_base,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_llm_for_request(llm_config: dict[str, Any] | None = None) -> BaseChatModel:
    """Resolve an LLM for a single agent run using the bounded request cache."""
    # Resolve factory symbols at call time so existing agent.llm patch points remain effective.
    from agent import llm as llm_module

    if llm_config is None:
        return llm_module.get_llm()

    key = _cache_key(
        llm_config["provider"],
        llm_config["model"],
        llm_config.get("api_key", ""),
        api_base=llm_config.get("api_base"),
        max_tokens=llm_config.get("max_tokens", 4096),
        temperature=llm_config.get("temperature", 0.0),
        reasoning_effort=llm_config.get("reasoning_effort"),
    )

    with _llm_cache_lock:
        if key in _llm_cache:
            _llm_cache.move_to_end(key)
            return _llm_cache[key]

    # Build outside the lock to avoid blocking other coroutines.
    llm = llm_module.create_llm_from_config(llm_config)
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
