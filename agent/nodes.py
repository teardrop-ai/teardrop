# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Graph node implementations for the Teardrop LangGraph agent.

Node pipeline:
  planner  →  (tool_executor ↩)  →  ui_generator  →  END
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.llm import extract_usage, get_llm
from agent.state import A2UIComponent, AgentState, TaskStatus
from config import get_settings
from tools import registry

logger = logging.getLogger(__name__)

# ─── Tool caches ──────────────────────────────────────────────────────────────

_cached_tools: list | None = None
_cached_tools_by_name: dict | None = None


def _get_cached_tools() -> list:
    global _cached_tools
    if _cached_tools is None:
        _cached_tools = registry.to_langchain_tools()
    return _cached_tools


def _get_cached_tools_by_name() -> dict:
    global _cached_tools_by_name
    if _cached_tools_by_name is None:
        _cached_tools_by_name = registry.get_langchain_tools_by_name()
    return _cached_tools_by_name


# ─── System prompts ───────────────────────────────────────────────────────────

_PLANNER_SYSTEM = """\
You are Teardrop, an intelligent task manager agent. Your job is to help users
plan and execute complex tasks. You have access to a suite of tools — use them
when the user's request requires data retrieval, calculation, or external calls.

When a task requires specialist capabilities beyond your own tools, you may
delegate it to a remote agent using the delegate_to_agent tool. Only delegate
when your own tools cannot handle the request.

After gathering all needed information, decide whether the response is best
presented as:
  1. Plain conversational text (for simple answers)
  2. A structured UI (for lists, tables, forms, progress trackers, etc.)

If a structured UI would improve comprehension, include a JSON block formatted
exactly like this anywhere in your final assistant message:

```a2ui
{"components": [<A2UIComponent>, ...]}
```

A2UI component types (use only these primitives):
  - text:     {"type":"text","props":{"content":"...","variant":"body|heading|caption"}}
  - table:    {"type":"table","props":{"columns":[...],"rows":[[...]]}}
  - columns:  {"type":"columns","children":[...]}
  - rows:     {"type":"rows","children":[...]}
  - form:     {"type":"form","props":{"fields":[...],"submit_label":"..."}}
  - button:   {"type":"button","props":{"label":"...","action":"..."}}
  - progress: {"type":"progress","props":{"value":0-100,"label":"..."}}

Keep payloads clean and data-bound. Never invent data you don't have.
"""

_UI_GENERATOR_SYSTEM = """\
You are a UI layout assistant. The agent has finished its reasoning.
Review the final assistant message below and extract any ```a2ui``` block.
If no a2ui block is present but the response contains structured data (lists,
numbers, comparisons) that would benefit from a visual presentation, generate
one. Output ONLY valid JSON matching the schema:
{"components": [<A2UIComponent>, ...]}
No markdown, no prose — pure JSON.
"""


# ─── Nodes ────────────────────────────────────────────────────────────────────


async def planner_node(state: AgentState) -> dict[str, Any]:
    """Reasoning / planning node.  Calls the LLM with bound tools."""
    logger.debug("planner_node: entry, %d messages", len(state.messages))
    settings = get_settings()
    tools = _get_cached_tools()
    org_tools = state.metadata.get("_org_tools", [])
    all_tools = tools + org_tools
    llm = get_llm().bind_tools(all_tools)  # type: ignore[arg-type]

    # ── Inject retrieved memories into the system prompt ──────────────────
    system_prompt = _PLANNER_SYSTEM
    memories: list[str] = state.metadata.get("_memories", [])
    if memories:
        # Guard against prompt injection from stored memory content.
        sanitized = [m.replace("```", "---") for m in memories]
        memory_block = "\n".join(f"- {m}" for m in sanitized)
        system_prompt += (
            "\n\n## Relevant Context from Memory\n"
            "The following facts were recalled from previous interactions with this organisation. "
            "Use them as background context only — do not repeat them verbatim unless asked.\n"
            f"{memory_block}"
        )
    messages = [SystemMessage(content=system_prompt), *state.messages]

    try:
        response: AIMessage = await asyncio.wait_for(  # type: ignore[assignment]
            llm.ainvoke(messages),
            timeout=settings.agent_llm_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.error(
            "planner_node: LLM call timed out after %ss",
            settings.agent_llm_timeout_seconds,
        )
        return {
            "messages": [AIMessage(content="The AI model timed out. Please try again.")],
            "task_status": TaskStatus.FAILED,
            "error": "LLM timeout",
        }
    except Exception as exc:
        logger.error("planner_node error: %s", exc)
        return {
            "messages": [AIMessage(content=f"I encountered an error: {exc}")],
            "task_status": TaskStatus.FAILED,
            "error": str(exc),
        }

    # ── Accumulate token usage ────────────────────────────────────────────
    usage = dict(state.metadata.get("_usage", {}))
    extracted = extract_usage(response)
    usage["tokens_in"] = usage.get("tokens_in", 0) + extracted["tokens_in"]
    usage["tokens_out"] = usage.get("tokens_out", 0) + extracted["tokens_out"]

    return {
        "messages": [response],
        "task_status": TaskStatus.EXECUTING if response.tool_calls else TaskStatus.GENERATING_UI,
        "metadata": {**state.metadata, "_usage": usage},
    }


async def _execute_single_tool(
    call: dict[str, Any], tools_by_name: dict
) -> tuple[ToolMessage, str]:
    """Execute one tool call; returns (ToolMessage, tool_name).

    Errors are caught per-tool so a single failure does not abort sibling calls.
    """
    tool_name: str = call["name"]
    tool_args: dict[str, Any] = call["args"]
    call_id: str = call["id"]

    tool = tools_by_name.get(tool_name)
    if tool is None:
        content = f"Tool '{tool_name}' not found."
    else:
        try:
            result = await tool.ainvoke(tool_args)
            content = json.dumps(result) if not isinstance(result, str) else result
        except Exception as exc:
            logger.warning("tool %s failed: %s", tool_name, exc)
            content = f"Tool error: {exc}"

    return ToolMessage(content=content, tool_call_id=call_id), tool_name


async def tool_executor_node(state: AgentState) -> dict[str, Any]:
    """Execute all pending tool calls in the latest AI message, in parallel."""
    logger.debug("tool_executor_node: entry")
    last_msg = state.messages[-1]
    if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
        return {"task_status": TaskStatus.GENERATING_UI}

    tools_by_name = {
        **_get_cached_tools_by_name(),
        **state.metadata.get("_org_tools_by_name", {}),
    }

    # ── Accumulate tool usage ─────────────────────────────────────────────
    usage = dict(state.metadata.get("_usage", {}))
    tool_names_acc: list[str] = list(usage.get("tool_names", []))

    results = await asyncio.gather(
        *[_execute_single_tool(call, tools_by_name) for call in last_msg.tool_calls]
    )
    tool_messages = [msg for msg, _ in results]
    tool_names_acc.extend(name for _, name in results)

    usage["tool_calls"] = usage.get("tool_calls", 0) + len(last_msg.tool_calls)
    usage["tool_names"] = tool_names_acc

    return {
        "messages": tool_messages,
        "task_status": TaskStatus.PLANNING,
        "metadata": {**state.metadata, "_usage": usage},
    }


async def ui_generator_node(state: AgentState) -> dict[str, Any]:
    """Parse or generate A2UI components from the agent's final message."""
    logger.debug("ui_generator_node: entry")
    last_msg = state.messages[-1] if state.messages else None

    # --- Try to extract inline ```a2ui``` block first ---
    if isinstance(last_msg, AIMessage) and last_msg.content:
        text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
        components = _extract_a2ui_from_text(text)
        if components:
            return {
                "ui_components": components,
                "task_status": TaskStatus.COMPLETED,
            }

    # --- If no inline block and we have a data-rich response, ask LLM to generate ---
    if isinstance(last_msg, AIMessage) and last_msg.content:
        text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
        if _contains_structured_data(text):
            settings = get_settings()
            prompt = f"{_UI_GENERATOR_SYSTEM}\n\nAssistant message:\n{text}"
            try:
                result: AIMessage = await asyncio.wait_for(  # type: ignore[assignment]
                    get_llm().ainvoke(prompt),
                    timeout=settings.agent_ui_generator_timeout_seconds,
                )
                raw = result.content if isinstance(result.content, str) else str(result.content)
                components = _parse_a2ui_json(raw)
                if components:
                    return {
                        "ui_components": components,
                        "task_status": TaskStatus.COMPLETED,
                    }
            except asyncio.TimeoutError:
                logger.warning("ui_generator_node: LLM call timed out")
            except Exception as exc:
                logger.warning("ui_generator_node: LLM call failed: %s", exc)

    return {"task_status": TaskStatus.COMPLETED}


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
