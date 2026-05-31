# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""A2UI surface generation node for the Teardrop LangGraph agent.

Extracted verbatim from ``agent.nodes``. ``ui_generator_node`` parses an inline
```a2ui``` block from the agent's final message, or (when none is present and the
response is data-rich) asks an LLM to synthesise A2UI components. The node and
its helpers are re-exported from ``agent.nodes`` for backward compatibility, so
existing imports and ``agent.graph`` continue to resolve unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.messages import AIMessage

from agent.llm import create_llm_from_config, get_llm_for_request
from agent.state import A2UIComponent, AgentState, TaskStatus
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


_UI_GENERATOR_SYSTEM = """\
You are a UI layout assistant. The agent has finished its reasoning.
Review the final assistant message below and extract any ```a2ui``` block.
If no a2ui block is present but the response contains structured data (lists,
numbers, comparisons) that would benefit from a visual presentation, generate
one. Output ONLY valid JSON matching the schema:
{"components": [<A2UIComponent>, ...]}
No markdown, no prose — pure JSON.
"""


async def ui_generator_node(state: AgentState) -> dict[str, Any]:
    """Parse or generate A2UI components from the agent's final message."""
    logger.debug("ui_generator_node: entry")
    last_msg = state.messages[-1] if state.messages else None
    if not isinstance(last_msg, AIMessage):
        for msg in reversed(state.messages):
            if isinstance(msg, AIMessage) and msg.content:
                last_msg = msg
                break
    emit_ui = bool(state.metadata.get("emit_ui", True))

    # --- Try to extract inline ```a2ui``` block first ---
    if isinstance(last_msg, AIMessage) and last_msg.content:
        text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
        components = _extract_a2ui_from_text(text)
        if components:
            return {
                "ui_components": [c.model_dump() for c in components],
                "task_status": TaskStatus.COMPLETED,
            }

    # --- If no inline block and we have a data-rich response, ask LLM to generate ---
    if emit_ui and isinstance(last_msg, AIMessage) and last_msg.content:
        text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
        if _contains_structured_data(text):
            settings = get_settings()
            prompt = f"{_UI_GENERATOR_SYSTEM}\n\nAssistant message:\n{text}"
            try:
                llm_config = state.metadata.get("_llm_config")
                if llm_config:
                    ui_llm = get_llm_for_request(llm_config)
                else:
                    ui_provider = settings.agent_ui_generator_provider
                    ui_model = settings.agent_ui_generator_model
                    ui_llm = create_llm_from_config(
                        {
                            "provider": ui_provider,
                            "model": ui_model,
                            "api_key": _provider_api_key(settings, ui_provider),
                            "max_tokens": settings.agent_synthesis_max_tokens,
                            "temperature": settings.agent_temperature,
                            "timeout_seconds": settings.agent_ui_generator_timeout_seconds,
                        }
                    )
                result: AIMessage = await asyncio.wait_for(  # type: ignore[assignment]
                    ui_llm.ainvoke(prompt),
                    timeout=settings.agent_ui_generator_timeout_seconds,
                )
                raw = result.content if isinstance(result.content, str) else str(result.content)
                components = _parse_a2ui_json(raw)
                if components:
                    return {
                        "ui_components": [c.model_dump() for c in components],
                        "task_status": TaskStatus.COMPLETED,
                    }
            except asyncio.TimeoutError:
                logger.warning("ui_generator_node: LLM call timed out")
            except Exception as exc:
                logger.warning("ui_generator_node: LLM call failed: %s", exc)

    return {"task_status": TaskStatus.COMPLETED}


def _provider_api_key(settings, provider: str) -> str:
    """Resolve the provider API key via ``agent.nodes`` (single source of truth)."""
    from agent.nodes import _provider_api_key as _resolve

    return _resolve(settings, provider)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _extract_a2ui_from_text(text: str) -> list[A2UIComponent]:
    """Extract components from a ```a2ui ... ``` fenced block."""
    import re

    pattern = r"```a2ui\s*(\{.*?\})\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return []
    return _parse_a2ui_json(match.group(1))


def _parse_a2ui_json(raw: str) -> list[A2UIComponent]:
    """Parse raw JSON string into A2UIComponent list."""
    try:
        data = json.loads(raw.strip())
        raw_components = data.get("components", [])
        return [A2UIComponent(**c) for c in raw_components]
    except Exception as exc:
        logger.debug("_parse_a2ui_json failed: %s", exc)
        return []


def _contains_structured_data(text: str) -> bool:
    """Heuristic: does the text contain tables, lists, or numeric data?"""
    import re

    indicators = [
        r"\|.*\|",  # Markdown table
        r"^\s*[-*]\s+",  # Bullet list
        r"\d+\.\s+\w+",  # Numbered list
        r"\b\d+[.,]\d+\b",  # Decimal numbers
    ]
    for pattern in indicators:
        if re.search(pattern, text, re.MULTILINE):
            return True
    return False
