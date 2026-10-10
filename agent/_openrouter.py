# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""OpenRouter chat model that preserves upstream billing metadata.

OpenRouter reports the concrete model that served the request (aliases such
as ``~google/gemini-flash-latest`` resolve to a dated model), the upstream
provider, and the exact charged cost in ``usage.cost``. Stock ``ChatOpenAI``
drops ``cost`` when streaming and concatenates ``model`` across chunks, so this
subclass copies all three onto ``response_metadata`` under ``upstream_*`` keys
exactly once per response (from the usage-bearing chunk when streaming).
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI


def upstream_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract ``upstream_*`` metadata from an OpenRouter response or usage chunk."""
    meta: dict[str, Any] = {}
    usage = payload.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("cost"), (int, float)):
        meta["upstream_cost_usd"] = float(usage["cost"])
    if isinstance(payload.get("model"), str) and payload["model"]:
        meta["upstream_model"] = payload["model"]
    if isinstance(payload.get("provider"), str) and payload["provider"]:
        meta["upstream_provider"] = payload["provider"]
    return meta


class ChatOpenRouter(ChatOpenAI):
    """``ChatOpenAI`` against OpenRouter with ``upstream_*`` response metadata."""

    def _convert_chunk_to_generation_chunk(self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None):
        generation_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if generation_chunk is not None and isinstance(chunk.get("usage"), dict):
            generation_chunk.message.response_metadata.update(upstream_metadata(chunk))
        return generation_chunk

    def _create_chat_result(self, response: Any, generation_info: dict | None = None):
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump(warnings=False)
        meta = upstream_metadata(response_dict)
        if meta:
            for generation in result.generations:
                generation.message.response_metadata.update(meta)
        return result
