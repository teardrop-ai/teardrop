# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Provider-independent LLM usage normalization."""

from __future__ import annotations

from langchain_core.messages import AIMessage


def extract_usage(response: AIMessage) -> dict[str, int | str]:
    """Extract ``tokens_in`` / ``tokens_out`` from an LLM response.

    Different providers use different key names in ``usage_metadata``:
    - Anthropic: ``input_tokens``, ``output_tokens``
    - OpenAI: ``input_tokens``, ``output_tokens`` (LangChain normalises)
    - Google: ``input_tokens``, ``output_tokens`` (LangChain normalises)

    LangChain >= 0.1.45 normalises most providers to ``input_tokens`` /
    ``output_tokens``, so this helper is forward-compatible. It also handles
    the legacy OpenAI ``prompt_tokens`` / ``completion_tokens`` keys as a
    fallback.
    """
    if not hasattr(response, "usage_metadata") or not response.usage_metadata:
        finish_reason = "stop"
        response_meta = getattr(response, "response_metadata", None)
        if isinstance(response_meta, dict):
            finish_reason = str(response_meta.get("finish_reason") or response_meta.get("stop_reason") or "stop")
        return {
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "finish_reason": finish_reason,
        }

    meta = response.usage_metadata

    tokens_in = meta.get("input_tokens") or meta.get("prompt_tokens") or 0
    tokens_out = meta.get("output_tokens") or meta.get("completion_tokens") or 0
    cache_read = 0
    cache_creation = 0

    # Anthropic usage metadata keys.
    cache_read = int(meta.get("cache_read_input_tokens") or 0)
    cache_creation = int(meta.get("cache_creation_input_tokens") or 0)

    # OpenAI prompt cache metadata (LangChain/OpenAI normalisation).
    prompt_details = meta.get("prompt_tokens_details") or {}
    if isinstance(prompt_details, dict):
        cache_read = max(cache_read, int(prompt_details.get("cached_tokens") or 0))

    finish_reason = "stop"
    response_meta = getattr(response, "response_metadata", None)
    if isinstance(response_meta, dict):
        finish_reason = str(response_meta.get("finish_reason") or response_meta.get("stop_reason") or "stop")

    return {
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cache_read_input_tokens": int(cache_read),
        "cache_creation_input_tokens": int(cache_creation),
        "finish_reason": finish_reason,
    }
