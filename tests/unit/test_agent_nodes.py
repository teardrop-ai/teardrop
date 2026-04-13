"""Unit tests for agent/nodes.py — planner, tool_executor, ui_generator nodes
and their helper functions.

No real LLM calls or tool executions; all external interactions are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.nodes as nodes_module
from agent.nodes import (
    _contains_structured_data,
    _extract_a2ui_from_text,
    _parse_a2ui_json,
    planner_node,
    tool_executor_node,
    ui_generator_node,
)
from agent.state import AgentState, TaskStatus

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_state(**overrides) -> AgentState:
    defaults = dict(
        messages=[HumanMessage(content="What is 2+2?")],
        metadata={"_usage": {}},
    )
    defaults.update(overrides)
    return AgentState(**defaults)


def _make_ai_message(content: str = "Hello", tool_calls: list | None = None) -> AIMessage:
    msg = MagicMock(spec=AIMessage)
    msg.content = content
    msg.tool_calls = tool_calls or []
    msg.usage_metadata = None
    msg.type = "ai"
    msg.id = "msg-id-1"
    msg.additional_kwargs = {}
    msg.response_metadata = {}
    return msg


# ─── _extract_a2ui_from_text ──────────────────────────────────────────────────


class TestExtractA2uiFromText:
    def test_valid_a2ui_block_is_extracted(self):
        text = '```a2ui\n{"components": [{"type": "text", "props": {"content": "hi"}}]}\n```'
        result = _extract_a2ui_from_text(text)
        assert len(result) == 1
        assert result[0].type == "text"

    def test_no_a2ui_block_returns_empty_list(self):
        result = _extract_a2ui_from_text("Just plain text, no fenced block.")
        assert result == []

    def test_malformed_json_returns_empty_list(self):
        text = "```a2ui\n{this is not valid json}\n```"
        result = _extract_a2ui_from_text(text)
        assert result == []

    def test_empty_components_array_returns_empty_list(self):
        text = '```a2ui\n{"components": []}\n```'
        result = _extract_a2ui_from_text(text)
        assert result == []

    def test_multiple_component_types(self):
        text = (
            "```a2ui\n"
            '{"components": ['
            '{"type": "text", "props": {"content": "Title"}}, '
            '{"type": "button", "props": {"label": "OK"}}'
            "]}\n```"
        )
        result = _extract_a2ui_from_text(text)
        assert len(result) == 2
        assert result[0].type == "text"
        assert result[1].type == "button"


# ─── _parse_a2ui_json ─────────────────────────────────────────────────────────


class TestParseA2uiJson:
    def test_valid_json_returns_components(self):
        raw = '{"components": [{"type": "progress", "props": {"value": 50}}]}'
        result = _parse_a2ui_json(raw)
        assert len(result) == 1
        assert result[0].type == "progress"

    def test_invalid_json_returns_empty_list(self):
        result = _parse_a2ui_json("not json at all")
        assert result == []

    def test_missing_components_key_returns_empty_list(self):
        result = _parse_a2ui_json('{"data": []}')
        assert result == []


# ─── _contains_structured_data ───────────────────────────────────────────────


class TestContainsStructuredData:
    def test_markdown_table_detected(self):
        assert _contains_structured_data("| Col1 | Col2 |\n|------|------|\n| a | b |")

    def test_bullet_list_detected(self):
        assert _contains_structured_data("- item one\n- item two")

    def test_numbered_list_detected(self):
        assert _contains_structured_data("1. First item\n2. Second item")

    def test_decimal_number_detected(self):
        assert _contains_structured_data("The value is 3.14 units.")

    def test_plain_text_not_detected(self):
        assert not _contains_structured_data("This is a simple sentence with no structure.")


# ─── planner_node ─────────────────────────────────────────────────────────────


class TestPlannerNode:
    async def test_no_tool_calls_routes_to_generating_ui(self, test_settings):
        mock_response = _make_ai_message("Here is my answer.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state()
        with (
            patch("agent.nodes.get_llm", return_value=mock_llm),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert len(result["messages"]) == 1

    async def test_tool_calls_routes_to_executing(self, test_settings):
        tool_call = {"id": "call-1", "name": "calculate", "args": {"expression": "2+2"}}
        mock_response = _make_ai_message("Let me calculate that.", tool_calls=[tool_call])
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state()
        with (
            patch("agent.nodes.get_llm", return_value=mock_llm),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.EXECUTING

    async def test_llm_timeout_returns_failed_status(self, test_settings):
        import asyncio

        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.ainvoke = AsyncMock(side_effect=asyncio.TimeoutError())

        state = _make_state()
        with (
            patch("agent.nodes.get_llm", return_value=mock_llm),
            patch.object(nodes_module, "_cached_tools", []),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.FAILED
        assert result["error"] == "LLM timeout"

    async def test_usage_metadata_accumulated(self, test_settings):
        mock_response = _make_ai_message("Answer.", tool_calls=[])
        mock_response.usage_metadata = {"input_tokens": 50, "output_tokens": 25}
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state(metadata={"_usage": {"tokens_in": 10, "tokens_out": 5}})
        with (
            patch("agent.nodes.get_llm", return_value=mock_llm),
            patch.object(nodes_module, "_cached_tools", []),
        ):
            result = await planner_node(state)

        usage = result["metadata"]["_usage"]
        assert usage["tokens_in"] == 60  # 10 + 50
        assert usage["tokens_out"] == 30  # 5 + 25


# ─── tool_executor_node ───────────────────────────────────────────────────────


class TestToolExecutorNode:
    async def test_executes_tool_and_returns_tool_message(self, test_settings):
        tool_call = {"id": "call-1", "name": "calculate", "args": {"expression": "2+2"}}
        last_msg = _make_ai_message(tool_calls=[tool_call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": 4.0})

        state = _make_state(messages=[last_msg])
        with patch.object(nodes_module, "_cached_tools_by_name", {"calculate": mock_tool}):
            result = await tool_executor_node(state)

        assert len(result["messages"]) == 1
        assert isinstance(result["messages"][0], ToolMessage)
        assert result["task_status"] == TaskStatus.PLANNING

    async def test_failed_tool_does_not_abort_other_tools(self, test_settings):
        """One failing tool should return an error ToolMessage, not raise."""
        calls = [
            {"id": "call-1", "name": "calculate", "args": {"expression": "1+1"}},
            {"id": "call-2", "name": "broken_tool", "args": {}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_calc = MagicMock()
        mock_calc.ainvoke = AsyncMock(return_value={"result": 2.0})

        state = _make_state(messages=[last_msg])
        with patch.object(
            nodes_module,
            "_cached_tools_by_name",
            {"calculate": mock_calc},
            # "broken_tool" is absent → tool_executor looks up None → returns error message
        ):
            result = await tool_executor_node(state)

        assert len(result["messages"]) == 2
        # Second message is an error for the missing tool
        content = result["messages"][1].content
        assert "not found" in content.lower() or "error" in content.lower()

    async def test_tool_call_count_accumulated_in_usage(self, test_settings):
        calls = [
            {"id": "c1", "name": "get_datetime", "args": {}},
            {"id": "c2", "name": "get_datetime", "args": {}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "now"})

        state = _make_state(
            messages=[last_msg],
            metadata={"_usage": {"tool_calls": 1, "tool_names": ["calculate"]}},
        )
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_datetime": mock_tool}):
            result = await tool_executor_node(state)

        usage = result["metadata"]["_usage"]
        assert usage["tool_calls"] == 3  # 1 existing + 2 new
        assert "get_datetime" in usage["tool_names"]

    async def test_no_tool_calls_returns_generating_ui(self, test_settings):
        """If the last message has no tool_calls the node skips to ui_generator."""
        last_msg = _make_ai_message(tool_calls=[])
        state = _make_state(messages=[last_msg])

        result = await tool_executor_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert "messages" not in result


# ─── ui_generator_node ────────────────────────────────────────────────────────


class TestUiGeneratorNode:
    async def test_extracts_inline_a2ui_block(self, test_settings):
        text = '```a2ui\n{"components": [{"type": "text", "props": {"content": "Done"}}]}\n```'
        last_msg = _make_ai_message(content=text)
        state = _make_state(messages=[last_msg])

        result = await ui_generator_node(state)

        assert result["task_status"] == TaskStatus.COMPLETED
        assert len(result["ui_components"]) == 1

    async def test_no_a2ui_block_still_completes(self, test_settings):
        """Even without a UI block the node should complete without error."""
        last_msg = _make_ai_message(content="Simple text answer, no numbers or lists.")
        state = _make_state(messages=[last_msg])

        # Patch _contains_structured_data to return False to skip LLM call
        with patch.object(nodes_module, "_contains_structured_data", return_value=False):
            # Also ensure get_llm is not called (no LLM needed)
            with patch("agent.llm.get_llm", return_value=None):
                result = await ui_generator_node(state)

        assert result["task_status"] == TaskStatus.COMPLETED

    async def test_malformed_a2ui_json_returns_no_components(self, test_settings):
        text = "```a2ui\n{bad json}\n```"
        last_msg = _make_ai_message(content=text)
        state = _make_state(messages=[last_msg])

        result = await ui_generator_node(state)

        # task_status should still be COMPLETED; ui_components absent or empty
        assert result["task_status"] == TaskStatus.COMPLETED
        assert result.get("ui_components", []) == []
