"""Graph node implementations for the Teardrop LangGraph agent.

Node pipeline:
  planner  →  (tool_executor ↩)  →  ui_generator  →  END
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.state import A2UIComponent, AgentState, TaskStatus
from config import get_settings
from tools import registry

logger = logging.getLogger(__name__)

# ─── LLM singleton ────────────────────────────────────────────────────────────

def _build_llm() -> ChatAnthropic:
    settings = get_settings()
    return ChatAnthropic(
        model=settings.agent_model,
        max_tokens=settings.agent_max_tokens,
        temperature=settings.agent_temperature,
        api_key=settings.anthropic_api_key or None,  # type: ignore[arg-type]
    )


# ─── System prompts ───────────────────────────────────────────────────────────

_PLANNER_SYSTEM = """\
You are Teardrop, an intelligent task manager agent. Your job is to help users
plan and execute complex tasks. You have access to a suite of tools — use them
when the user's request requires data retrieval, calculation, or external calls.

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
    tools = registry.to_langchain_tools()
    llm = _build_llm().bind_tools(tools)  # type: ignore[arg-type]

    messages = [SystemMessage(content=_PLANNER_SYSTEM), *state.messages]

    try:
        response: AIMessage = await llm.ainvoke(messages)  # type: ignore[assignment]
    except Exception as exc:
        logger.error("planner_node error: %s", exc)
        return {
            "messages": [AIMessage(content=f"I encountered an error: {exc}")],
            "task_status": TaskStatus.FAILED,
            "error": str(exc),
        }

    # ── Accumulate token usage ────────────────────────────────────────────
    usage = dict(state.metadata.get("_usage", {}))
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        usage["tokens_in"] = usage.get("tokens_in", 0) + response.usage_metadata.get("input_tokens", 0)
        usage["tokens_out"] = usage.get("tokens_out", 0) + response.usage_metadata.get("output_tokens", 0)

    return {
        "messages": [response],
        "task_status": TaskStatus.EXECUTING if response.tool_calls else TaskStatus.GENERATING_UI,
        "metadata": {**state.metadata, "_usage": usage},
    }


async def tool_executor_node(state: AgentState) -> dict[str, Any]:
    """Execute all pending tool calls in the latest AI message."""
    logger.debug("tool_executor_node: entry")
    last_msg = state.messages[-1]
    if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
        return {"task_status": TaskStatus.GENERATING_UI}

    tools_by_name = registry.get_langchain_tools_by_name()
    tool_messages: list[ToolMessage] = []

    # ── Accumulate tool usage ─────────────────────────────────────────────
    usage = dict(state.metadata.get("_usage", {}))
    tool_names_acc: list[str] = list(usage.get("tool_names", []))

    for call in last_msg.tool_calls:
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

        tool_messages.append(ToolMessage(content=content, tool_call_id=call_id))
        tool_names_acc.append(tool_name)

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
            llm = _build_llm()
            prompt = f"{_UI_GENERATOR_SYSTEM}\n\nAssistant message:\n{text}"
            try:
                result: AIMessage = await llm.ainvoke(prompt)  # type: ignore[assignment]
                raw = result.content if isinstance(result.content, str) else str(result.content)
                components = _parse_a2ui_json(raw)
                if components:
                    return {
                        "ui_components": components,
                        "task_status": TaskStatus.COMPLETED,
                    }
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
        r"\|.*\|",        # Markdown table
        r"^\s*[-*]\s+",   # Bullet list
        r"\d+\.\s+\w+",   # Numbered list
        r"\b\d+[.,]\d+\b",# Decimal numbers
    ]
    for pattern in indicators:
        if re.search(pattern, text, re.MULTILINE):
            return True
    return False
