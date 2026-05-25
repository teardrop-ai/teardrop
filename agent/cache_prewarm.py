# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Prompt-cache prewarm utilities."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agent.llm import create_llm_from_config, extract_usage
from agent.nodes import _build_cached_planner_prefix
from teardrop.config import get_settings
from tools import registry

logger = logging.getLogger(__name__)


def _provider_api_key(provider: str) -> str:
    s = get_settings()
    if provider == "anthropic":
        return s.anthropic_api_key or ""
    if provider == "openai":
        return s.openai_api_key or ""
    if provider == "google":
        return s.google_api_key or ""
    if provider == "openrouter":
        return s.openrouter_api_key or ""
    return ""


async def prewarm_org_prefix(
    org_id: str,
    provider: str,
    model: str,
    llm_config: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Prime provider prompt cache with a 1-token probe call.

    Returns extracted usage counters for telemetry aggregation.
    """
    settings = get_settings()
    cfg: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "api_key": (llm_config or {}).get("api_key") or _provider_api_key(provider),
        "api_base": (llm_config or {}).get("api_base"),
        "max_tokens": 1,
        "temperature": 0.0,
        "timeout_seconds": min(30, settings.agent_llm_timeout_seconds),
    }
    if not cfg["api_key"]:
        return {
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    prefix = _build_cached_planner_prefix(platform_tools=registry.to_langchain_tools(), emit_ui=True)
    llm = create_llm_from_config(cfg)

    system = SystemMessage(content=prefix)
    if provider == "anthropic":
        system = SystemMessage(content=prefix, additional_kwargs={"cache_control": {"type": "ephemeral"}})

    try:
        response = await llm.ainvoke([system, HumanMessage(content="cache warmup probe")])
        usage = extract_usage(response)
        logger.debug("cache_prewarm: org=%s provider=%s model=%s usage=%s", org_id, provider, model, usage)
        return usage
    except Exception:
        logger.debug("cache_prewarm failed org=%s provider=%s model=%s", org_id, provider, model, exc_info=True)
        return {
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
