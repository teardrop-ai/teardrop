"""Unit tests for agent/nodes.py — planner, tool_executor, ui_generator nodes
and their helper functions.

No real LLM calls or tool executions; all external interactions are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.nodes as nodes_module
from agent.nodes import (
    _contains_structured_data,
    _extract_a2ui_from_text,
    _max_iterations_reached,
    _parse_a2ui_json,
    _planner_signaled_done,
    _synthesis_fast_path_reason,
    planner_node,
    tool_executor_node,
    ui_generator_node,
)
from agent.runtime_context import AgentRunContext, agent_run_context
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


@pytest.fixture(autouse=True)
def _empty_agent_run_context():
    with agent_run_context(AgentRunContext([], {})):
        yield


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


class TestGoogleSchemaValidation:
    def test_array_items_anyof_without_type_is_rejected(self):
        class _ArgsSchema:
            @staticmethod
            def model_json_schema():
                return {
                    "type": "object",
                    "properties": {
                        "args": {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},
                                    {"type": "integer"},
                                ]
                            },
                        }
                    },
                }

        class _Tool:
            name = "bad_tool"
            args_schema = _ArgsSchema

        from agent.nodes import _validate_tools_for_google

        try:
            _validate_tools_for_google([_Tool()])
            assert False, "Expected ValueError for Gemini-incompatible array items schema"
        except ValueError as exc:
            assert "Gemini" in str(exc)


class TestSynthesisFastPathPredicates:
    def test_planner_signaled_done_true_on_text_without_tools(self):
        state = _make_state(messages=[_make_ai_message(content="Done.", tool_calls=[])])
        assert _planner_signaled_done(state) is True

    def test_planner_signaled_done_false_with_tool_calls(self):
        state = _make_state(messages=[_make_ai_message(content="Thinking", tool_calls=[{"id": "c1", "name": "t", "args": {}}])])
        assert _planner_signaled_done(state) is False

    def test_max_iterations_reached_guard(self, test_settings):
        state = _make_state(metadata={"_usage": {"tool_iterations": test_settings.agent_max_tool_iterations - 1}})
        assert _max_iterations_reached(state) is True

    def test_completed_tool_batch_does_not_force_synthesis(self, test_settings):
        ai = _make_ai_message(
            content="Calling tools",
            tool_calls=[
                {"id": "call-1", "name": "get_a", "args": {}},
            ],
        )
        state = _make_state(
            messages=[
                HumanMessage(content="hi"),
                ai,
                ToolMessage(content="{}", tool_call_id="call-1"),
            ],
            metadata={"_usage": {"tool_iterations": 1}},
        )
        assert _synthesis_fast_path_reason(state) is None

    def test_stablecoin_basket_batch_uses_synthesis_fast_path(self, test_settings):
        symbols = [
            "LUSD",
            "BOLD",
            "crvUSD",
            "GHO",
            "DAI",
            "USDT",
            "USDC",
            "PYUSD",
            "RLUSD",
            "USDG",
            "USD1",
            "FDUSD",
            "TUSD",
            "USDP",
            "USDS",
        ]
        state = _make_state(
            messages=[
                HumanMessage(content="Compare stablecoin yields"),
                _make_ai_message(
                    "",
                    tool_calls=[
                        {
                            "id": "yield-call",
                            "name": "get_yield_rates",
                            "args": {
                                "stable_only": True,
                                "max_apy": 30,
                                "min_tvl_usd": 1_000_000,
                                "limit": 50,
                                "symbols_any": symbols,
                            },
                        },
                        {
                            "id": "price-call",
                            "name": "get_token_price",
                            "args": {"tokens": symbols, "vs_currency": "usd"},
                        },
                    ],
                ),
                ToolMessage(content="{}", tool_call_id="yield-call"),
                ToolMessage(content="{}", tool_call_id="price-call"),
            ],
            metadata={"_usage": {"tool_iterations": 1}},
        )

        assert _synthesis_fast_path_reason(state) == "stablecoin_basket"


class TestToolShortlistHook:
    def test_apply_tool_shortlist_noop(self):
        from agent.nodes import _apply_tool_shortlist

        platform_tools = [MagicMock(name="calculate")]
        org_tools = [MagicMock(name="org_tool")]
        all_tools = platform_tools + org_tools

        shortlisted_all, shortlisted_platform, shortlisted_org = _apply_tool_shortlist(
            all_tools=all_tools,
            platform_tools=platform_tools,
            org_tools=org_tools,
        )

        assert shortlisted_all == all_tools
        assert shortlisted_platform == platform_tools
        assert shortlisted_org == org_tools


# ─── planner_node ─────────────────────────────────────────────────────────────


class TestPlannerNode:
    async def test_post_tool_planner_can_execute_second_tool_round(self, test_settings):
        prior_ai = _make_ai_message(
            "Gathering data",
            tool_calls=[{"id": "call-1", "name": "get_a", "args": {}}],
        )
        state = _make_state(
            messages=[
                HumanMessage(content="Compare the results"),
                prior_ai,
                ToolMessage(content="{}", tool_call_id="call-1"),
            ],
            metadata={"_usage": {"tool_iterations": 1, "tool_names": ["get_a"]}},
        )
        second_call = _make_ai_message(
            "",
            tool_calls=[{"id": "call-2", "name": "get_b", "args": {}}],
        )
        base_llm = MagicMock()
        base_llm.bind_tools.return_value = base_llm
        synthesis_llm = MagicMock()
        synthesis_llm.bind_tools.return_value = synthesis_llm
        synthesis_llm.ainvoke = AsyncMock(return_value=second_call)
        tool = MagicMock()
        tool.ainvoke = AsyncMock(return_value={"value": 2})

        with (
            patch("agent.nodes.get_llm_for_request", side_effect=[base_llm, synthesis_llm]),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch.object(nodes_module, "_cached_tools", [tool]),
        ):
            planner_result = await planner_node(state)

        assert planner_result["task_status"] == TaskStatus.EXECUTING
        synthesis_llm.bind_tools.assert_called_once()

        executor_state = _make_state(
            messages=[*state.messages, *planner_result["messages"]],
            metadata=planner_result["metadata"],
        )
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_b": tool}):
            executor_result = await tool_executor_node(executor_state)

        assert executor_result["task_status"] == TaskStatus.PLANNING
        assert executor_result["metadata"]["_usage"]["tool_iterations"] == 2

    async def test_excluded_tools_not_bound_to_llm(self, test_settings):
        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = f"{name} description"

        captured: dict[str, list] = {}
        mock_response = _make_ai_message("No tools needed.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        def _bind_spy(llm, tools, provider):
            captured["tools"] = list(tools)
            return llm

        state = _make_state(
            metadata={
                "_usage": {},
                "_org_tools": [_Tool("org_allowed"), _Tool("org_blocked")],
                "_excluded_tool_names": ["web_search", "org_blocked"],
            }
        )
        with (
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes._bind_tools_for_provider", side_effect=_bind_spy),
            patch.object(nodes_module, "_cached_tools", [_Tool("web_search"), _Tool("calculate")]),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        bound_names = [tool.name for tool in captured["tools"]]
        assert "web_search" not in bound_names
        assert "org_blocked" not in bound_names
        assert "calculate" in bound_names

    async def test_planner_calls_tool_shortlist_hook(self, test_settings):
        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = f"{name} description"

        captured: dict[str, list[str]] = {}
        mock_response = _make_ai_message("No tools needed.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        def _shortlist_spy(*, all_tools, platform_tools, org_tools):
            captured["all"] = [t.name for t in all_tools]
            captured["platform"] = [t.name for t in platform_tools]
            captured["org"] = [t.name for t in org_tools]
            return all_tools, platform_tools, org_tools

        org_tool = _Tool("org_custom_tool")
        state = _make_state(metadata={"_usage": {}})
        with (
            agent_run_context(AgentRunContext([org_tool], {})),
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes._bind_tools_for_provider", side_effect=lambda llm, tools, provider: llm),
            patch("agent.nodes._apply_tool_shortlist", side_effect=_shortlist_spy) as shortlist_mock,
            patch.object(nodes_module, "_cached_tools", [_Tool("calculate")]),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        shortlist_mock.assert_called_once()
        assert captured["platform"] == ["calculate"]
        assert captured["org"] == ["org_custom_tool"]
        assert sorted(captured["all"]) == ["calculate", "org_custom_tool"]

    async def test_excluded_tools_not_listed_in_system_prompt(self, test_settings):
        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = f"{name} description"

        captured: dict[str, list] = {}
        mock_response = _make_ai_message("No tools needed.", tool_calls=[])
        mock_llm = MagicMock()

        async def _invoke_spy(llm, messages, timeout_seconds, *, provider=None, model=None):
            captured["messages"] = list(messages)
            return mock_response

        org_tools = [_Tool("org_allowed"), _Tool("org_blocked")]
        state = _make_state(
            metadata={
                "_usage": {},
                "_excluded_tool_names": ["web_search", "org_blocked"],
            }
        )
        with (
            agent_run_context(AgentRunContext(org_tools, {})),
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes._bind_tools_for_provider", side_effect=lambda llm, tools, provider: llm),
            patch("agent.nodes._invoke_planner_llm", side_effect=_invoke_spy),
            patch.object(nodes_module, "_cached_tools", [_Tool("web_search"), _Tool("calculate")]),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        system_text = "\n".join(str(msg.content) for msg in captured["messages"] if getattr(msg, "type", "") == "system")
        assert "- **web_search**:" not in system_text
        assert "- **org_blocked**:" not in system_text
        assert "- **calculate**:" in system_text
        assert "- **org_allowed**:" in system_text

    async def test_no_tool_calls_routes_to_generating_ui(self, test_settings):
        mock_response = _make_ai_message("Here is my answer.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state()
        with (
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
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
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
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
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
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
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        usage = result["metadata"]["_usage"]
        assert usage["tokens_in"] == 60  # 10 + 50
        assert usage["tokens_out"] == 30  # 5 + 25

    async def test_rate_limit_retries_with_fallback(self, test_settings):
        primary_llm = MagicMock()
        primary_llm.bind_tools.return_value = primary_llm
        primary_llm.ainvoke = AsyncMock(side_effect=Exception("429 rate limit"))

        fallback_response = _make_ai_message("Recovered answer.", tool_calls=[])
        fallback_llm = MagicMock()
        fallback_llm.bind_tools.return_value = fallback_llm
        fallback_llm.ainvoke = AsyncMock(return_value=fallback_response)

        state = _make_state()
        with (
            patch("agent.nodes.get_llm_for_request", return_value=primary_llm),
            patch("agent.nodes._get_fallback_llm", return_value=(fallback_llm, "anthropic", "claude-sonnet-4-6")),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert fallback_llm.ainvoke.call_count == 1

    async def test_rate_limit_synthesis_fallback_bounds_reasoning_effort(self, test_settings):
        primary_llm = MagicMock()
        primary_llm.bind_tools.return_value = primary_llm
        primary_llm.ainvoke = AsyncMock(side_effect=Exception("429 rate limit"))

        fallback_response = _make_ai_message("Recovered answer.", tool_calls=[])
        fallback_llm = MagicMock()
        fallback_llm.bind_tools.return_value = fallback_llm
        fallback_llm.ainvoke = AsyncMock(return_value=fallback_response)

        state = _make_state(metadata={"_usage": {"tool_iterations": 1, "tool_names": ["get_yield_rates"]}})
        with (
            patch("agent.nodes.get_llm_for_request", return_value=primary_llm),
            patch("agent.nodes.create_llm_from_config", return_value=primary_llm),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch(
                "agent.nodes._get_fallback_llm",
                return_value=(fallback_llm, "google", "gemini-3.6-flash"),
            ) as mock_get_fallback,
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        mock_get_fallback.assert_called_once_with(
            failed_provider="openrouter",
            failed_model=test_settings.agent_model,
            max_tokens=test_settings.agent_synthesis_max_tokens,
            reasoning_effort="none",
        )
        fallback_llm.ainvoke.assert_awaited_once()

    async def test_synthesis_retry_uses_rate_limit_fallback_llm(self, test_settings):
        primary_llm = MagicMock()
        primary_llm.bind_tools.return_value = primary_llm
        primary_llm.ainvoke = AsyncMock(side_effect=Exception("429 rate limit"))

        tool_call_response = _make_ai_message(
            "",
            tool_calls=[{"id": "call-2", "name": "get_yield_rates", "args": {}}],
        )
        final_response = _make_ai_message("Recovered synthesis.", tool_calls=[])
        fallback_llm = MagicMock()
        fallback_llm.bind_tools.return_value = fallback_llm
        fallback_llm.ainvoke = AsyncMock(side_effect=[tool_call_response, final_response])

        state = _make_state(metadata={"_usage": {"tool_iterations": 1, "tool_names": ["get_yield_rates"]}})
        with (
            patch("agent.nodes.get_llm_for_request", return_value=primary_llm),
            patch("agent.nodes.create_llm_from_config", return_value=primary_llm),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._get_fallback_llm", return_value=(fallback_llm, "google", "gemini-3.6-flash")),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert result["messages"][0].content == "Recovered synthesis."
        primary_llm.ainvoke.assert_awaited_once()
        assert fallback_llm.ainvoke.await_count == 2

    async def test_preemptive_cooldown_uses_fallback_without_lc_kwargs(self, test_settings):
        """Regression: planner_node must not access lc_kwargs on the fallback LLM.

        Previously crashed with AttributeError when the primary provider was in
        cooldown and the selected fallback was a ChatAnthropic instance (which
        has no lc_kwargs attribute).
        """
        fallback_response = _make_ai_message("Fallback answer.", tool_calls=[])
        # Use spec=object so that accessing any attribute not on object raises
        # AttributeError — this would have caught the original lc_kwargs bug.
        fallback_llm = MagicMock(spec=object)
        fallback_llm.bind_tools = MagicMock(return_value=fallback_llm)
        fallback_llm.ainvoke = AsyncMock(return_value=fallback_response)

        state = _make_state()
        with (
            patch("agent.nodes.is_provider_cooled_down", return_value=True),
            patch("agent.nodes._get_fallback_llm", return_value=(fallback_llm, "anthropic", "claude-sonnet-4-6")),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert fallback_llm.ainvoke.call_count == 1

    async def test_rate_limit_with_org_llm_config_does_not_fallback(self, test_settings):
        primary_llm = MagicMock()
        primary_llm.bind_tools.return_value = primary_llm
        primary_llm.ainvoke = AsyncMock(side_effect=Exception("429 too many requests"))

        state = _make_state(
            metadata={
                "_usage": {},
                "_llm_config": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "is_byok": True,
                },
            }
        )
        with (
            patch("agent.nodes.get_llm_for_request", return_value=primary_llm),
            patch("agent.nodes._get_fallback_llm", return_value=None) as mock_fallback,
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.FAILED
        assert result["error_type"] == "rate_limit"
        mock_fallback.assert_not_called()

    async def test_synthesis_turn_uses_synthesis_max_tokens(self, test_settings):
        mock_response = _make_ai_message("Final concise synthesis", tool_calls=[])

        normal_llm = MagicMock()
        normal_llm.bind_tools.return_value = normal_llm

        synthesis_llm = MagicMock()
        synthesis_llm.bind_tools.return_value = synthesis_llm
        synthesis_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state(metadata={"_usage": {"tool_iterations": 1, "tool_names": ["get_wallet_portfolio"]}})
        with (
            patch("agent.nodes.get_llm_for_request", side_effect=[normal_llm, synthesis_llm]) as mock_get_llm,
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert mock_get_llm.call_count == 2
        cfg = mock_get_llm.call_args.args[0]
        assert cfg["max_tokens"] == test_settings.agent_synthesis_max_tokens
        # Reasoning is bounded so hidden reasoning tokens cannot consume the
        # shared output budget and truncate the visible synthesis (root cause of
        # the mid-JSON truncation regression).
        assert cfg["reasoning_effort"] == "none"

    async def test_truncated_synthesis_is_not_retried(self, test_settings):
        """A truncated synthesis response flows through untouched.

        Truncation is prevented upstream by bounding reasoning effort on the
        synthesis LLM; the planner no longer retries on a fallback model (the
        fallback shared the same token-budget mechanics and truncated too, at
        double cost and latency).
        """
        truncated_response = _make_ai_message('{"task_class":"stablecoin_yield_compare_test","stables":[')
        truncated_response.usage_metadata = {
            "input_tokens": 9000,
            "output_tokens": test_settings.agent_synthesis_max_tokens,
        }
        truncated_response.response_metadata = {"finish_reason": "length"}

        primary_llm = MagicMock()
        primary_llm.bind_tools.return_value = primary_llm
        primary_llm.ainvoke = AsyncMock(return_value=truncated_response)

        state = _make_state(metadata={"_usage": {"tool_iterations": 1, "tool_names": ["get_yield_rates"]}})
        with (
            patch("agent.nodes.get_llm_for_request", side_effect=[MagicMock(), primary_llm]),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._get_fallback_llm", return_value=None) as mock_get_fallback,
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert result["messages"][0].content == truncated_response.content
        assert "_synthesis_invalid" not in result["metadata"]
        mock_get_fallback.assert_not_called()

    async def test_synthesis_tool_calls_retry_emits_final_text(self, test_settings):
        """A post-tool synthesis turn that re-issues tool_calls is retried once with
        a no-tools instruction so the report text is emitted (GENERATING_UI) instead
        of being dropped by the router (EXECUTING discards buffered planner text)."""
        tool_call_response = _make_ai_message(
            "",
            tool_calls=[{"id": "call-2", "name": "get_yield_rates", "args": {}}],
        )
        tool_call_response.usage_metadata = {"input_tokens": 9000, "output_tokens": 50}

        final_response = _make_ai_message('{"task_class": "stablecoin_yield_compare_test", "stables": []}', tool_calls=[])
        final_response.usage_metadata = {"input_tokens": 9100, "output_tokens": 120}

        primary_llm = MagicMock()
        primary_llm.bind_tools.return_value = primary_llm
        # First synthesis call returns tool_calls; retry returns final text.
        primary_llm.ainvoke = AsyncMock(side_effect=[tool_call_response, final_response])

        state = _make_state(metadata={"_usage": {"tool_iterations": 1, "tool_names": ["get_yield_rates"]}})
        with (
            patch("agent.nodes.get_llm_for_request", side_effect=[MagicMock(), primary_llm]),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._get_fallback_llm", return_value=None),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert result["messages"][0].content == final_response.content
        assert primary_llm.ainvoke.await_count == 2
        # The retry appended the assistant tool_call message + no-tools system message.
        retry_messages = primary_llm.ainvoke.call_args.args[0]
        assert retry_messages[-1].content.startswith("You have already gathered all required tool outputs")
        # Both attempts are billed.
        assert result["metadata"]["_usage"]["tokens_out"] == 50 + 120

    async def test_synthesis_tool_calls_persisting_are_stripped_to_emit_text(self, test_settings):
        """If the no-tools retry STILL returns tool_calls, strip them and emit text
        (GENERATING_UI) rather than dropping the run."""
        tool_call_response = _make_ai_message(
            "partial",
            tool_calls=[{"id": "call-2", "name": "get_yield_rates", "args": {}}],
        )
        tool_call_response.usage_metadata = {"input_tokens": 9000, "output_tokens": 50}

        still_tool_calls = _make_ai_message(
            '{"task_class": "stablecoin_yield_compare_test"}',
            tool_calls=[{"id": "call-3", "name": "get_token_price", "args": {}}],
        )
        still_tool_calls.usage_metadata = {"input_tokens": 9100, "output_tokens": 80}

        primary_llm = MagicMock()
        primary_llm.bind_tools.return_value = primary_llm
        primary_llm.ainvoke = AsyncMock(side_effect=[tool_call_response, still_tool_calls])

        state = _make_state(metadata={"_usage": {"tool_iterations": 1, "tool_names": ["get_yield_rates"]}})
        with (
            patch("agent.nodes.get_llm_for_request", side_effect=[MagicMock(), primary_llm]),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._get_fallback_llm", return_value=None),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        # tool_calls stripped → GENERATING_UI so the text is emitted.
        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert result["messages"][0].tool_calls == []
        assert result["messages"][0].content == '{"task_class": "stablecoin_yield_compare_test"}'

    async def test_empty_synthesis_retry_keeps_tool_calls_for_executor(self, test_settings):
        tool_call_response = _make_ai_message(
            "",
            tool_calls=[{"id": "call-2", "name": "get_yield_rates", "args": {}}],
        )
        empty_retry = _make_ai_message(
            "",
            tool_calls=[{"id": "call-3", "name": "get_token_price", "args": {}}],
        )
        synthesis_llm = MagicMock()
        synthesis_llm.bind_tools.return_value = synthesis_llm
        synthesis_llm.ainvoke = AsyncMock(side_effect=[tool_call_response, empty_retry])

        state = _make_state(metadata={"_usage": {"tool_iterations": 1, "tool_names": ["get_yield_rates"]}})
        with (
            patch("agent.nodes.get_llm_for_request", side_effect=[MagicMock(), synthesis_llm]),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.EXECUTING
        assert result["messages"][0].tool_calls == empty_retry.tool_calls

    async def test_byok_synthesis_tool_calls_are_retried(self, test_settings):
        tool_call_response = _make_ai_message(
            "",
            tool_calls=[{"id": "call-2", "name": "get_yield_rates", "args": {}}],
        )
        final_response = _make_ai_message("BYOK synthesis.", tool_calls=[])
        byok_llm = MagicMock()
        byok_llm.bind_tools.return_value = byok_llm
        byok_llm.ainvoke = AsyncMock(side_effect=[tool_call_response, final_response])
        state = _make_state(
            metadata={
                "_usage": {"tool_iterations": 1, "tool_names": ["get_yield_rates"]},
                "_llm_config": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "api_key": "test-key",
                    "is_byok": True,
                },
            }
        )
        with (
            patch("agent.nodes.get_llm_for_request", return_value=byok_llm),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert result["messages"][0].content == "BYOK synthesis."
        assert byok_llm.ainvoke.await_count == 2

    async def test_initial_turn_uses_planner_override_model(self, test_settings):
        mock_response = _make_ai_message("Planner response", tool_calls=[])

        normal_llm = MagicMock()
        normal_llm.bind_tools.return_value = normal_llm

        planner_llm = MagicMock()
        planner_llm.bind_tools.return_value = planner_llm
        planner_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state(metadata={"_usage": {"tool_iterations": 0}})
        with (
            patch.object(test_settings, "agent_planner_provider", "google"),
            patch.object(test_settings, "agent_planner_model", "gemini-3-flash-preview"),
            patch("agent.nodes.get_llm_for_request", side_effect=[normal_llm, planner_llm]) as mock_get_llm,
            patch("agent.nodes._provider_api_key", return_value="test-key"),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert mock_get_llm.call_count == 2
        cfg = mock_get_llm.call_args.args[0]
        assert cfg["provider"] == "google"
        assert cfg["model"] == "gemini-3-flash-preview"
        assert cfg["reasoning_effort"] == "low"

    async def test_initial_turn_does_not_use_planner_override_with_org_llm_config(self, test_settings):
        mock_response = _make_ai_message("Planner response", tool_calls=[])
        normal_llm = MagicMock()
        normal_llm.bind_tools.return_value = normal_llm
        normal_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state(
            metadata={
                "_usage": {"tool_iterations": 0},
                "_llm_config": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "is_byok": True,
                },
            }
        )
        with (
            patch.object(test_settings, "agent_planner_provider", "google"),
            patch.object(test_settings, "agent_planner_model", "gemini-3-flash-preview"),
            patch("agent.nodes.get_llm_for_request", return_value=normal_llm),
            patch("agent.nodes.create_llm_from_config") as mock_create_from_config,
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        mock_create_from_config.assert_not_called()

    async def test_forced_synthesis_flag_is_cleared_after_planner_turn(self, test_settings):
        mock_response = _make_ai_message("Final answer", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state(metadata={"_usage": {"tool_iterations": 1}, "_synthesis_forced": True})
        with (
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes.create_llm_from_config", return_value=mock_llm),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["metadata"]["_synthesis_forced"] is False

    async def test_empty_forced_synthesis_emits_placeholder_text(self, test_settings):
        empty_response = _make_ai_message("", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.ainvoke = AsyncMock(return_value=empty_response)

        state = _make_state(metadata={"_usage": {"tool_iterations": 1}, "_synthesis_forced": True})
        with (
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert result["messages"][0].content

    async def test_synthesis_forced_uses_unbound_llm(self, test_settings):
        mock_response = _make_ai_message("Final answer", tool_calls=[])

        base_llm = MagicMock()
        base_llm.bind_tools.return_value = base_llm
        base_llm.ainvoke = AsyncMock(return_value=mock_response)

        unbound_llm = MagicMock()
        unbound_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state(metadata={"_usage": {"tool_iterations": 1}, "_synthesis_forced": True})
        with (
            patch("agent.nodes.get_llm_for_request", side_effect=[base_llm, unbound_llm]),
            patch("agent.nodes._bind_tools_for_provider") as bind_mock,
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert bind_mock.call_count == 1
        unbound_llm.ainvoke.assert_called_once()
        assert result["metadata"]["_synthesis_forced"] is False

    async def test_delegate_to_agent_excluded_when_a2a_disabled(self, test_settings):
        """When a2a_delegation_enabled=False, delegate_to_agent must not be bound."""

        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = f"{name} description"

        captured: dict[str, list] = {}
        mock_response = _make_ai_message("No tools needed.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        def _bind_spy(llm, tools, provider):
            captured["tools"] = list(tools)
            return llm

        state = _make_state(metadata={"_usage": {}})
        with (
            patch.object(test_settings, "a2a_delegation_enabled", False),
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes._bind_tools_for_provider", side_effect=_bind_spy),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._get_fallback_llm", return_value=None),
            patch.object(nodes_module, "_cached_tools", [_Tool("delegate_to_agent"), _Tool("calculate")]),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        bound_names = [tool.name for tool in captured["tools"]]
        assert "delegate_to_agent" not in bound_names
        assert "calculate" in bound_names

    async def test_delegate_to_agent_included_when_a2a_enabled(self, test_settings):
        """When a2a_delegation_enabled=True, delegate_to_agent must be bound."""

        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = f"{name} description"

        captured: dict[str, list] = {}
        mock_response = _make_ai_message("No tools needed.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        def _bind_spy(llm, tools, provider):
            captured["tools"] = list(tools)
            return llm

        state = _make_state(metadata={"_usage": {}})
        with (
            patch.object(test_settings, "a2a_delegation_enabled", True),
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes._bind_tools_for_provider", side_effect=_bind_spy),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._get_fallback_llm", return_value=None),
            patch.object(nodes_module, "_cached_tools", [_Tool("delegate_to_agent"), _Tool("calculate")]),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        bound_names = [tool.name for tool in captured["tools"]]
        assert "delegate_to_agent" in bound_names
        assert "calculate" in bound_names

    async def test_delegate_to_agent_absent_from_system_prompt_when_a2a_disabled(self, test_settings):
        """When a2a disabled, system prompt must not mention delegate_to_agent."""

        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = f"{name} description"

        captured: dict[str, list] = {}
        mock_response = _make_ai_message("No tools needed.", tool_calls=[])
        mock_llm = MagicMock()

        async def _invoke_spy(llm, messages, timeout_seconds, *, provider=None, model=None):
            captured["messages"] = list(messages)
            return mock_response

        state = _make_state(metadata={"_usage": {}})
        with (
            patch.object(test_settings, "a2a_delegation_enabled", False),
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes._bind_tools_for_provider", side_effect=lambda llm, tools, provider: llm),
            patch("agent.nodes._invoke_planner_llm", side_effect=_invoke_spy),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._get_fallback_llm", return_value=None),
            patch.object(nodes_module, "_cached_tools", [_Tool("delegate_to_agent"), _Tool("calculate")]),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        system_text = "\n".join(str(msg.content) for msg in captured["messages"] if getattr(msg, "type", "") == "system")
        assert "delegate_to_agent" not in system_text
        assert "calculate" in system_text


# ─── Request-scoped tool resolution ───────────────────────────────────────────


class TestPlannerNodeRunContextTools:
    async def test_planner_node_org_tools_from_run_context(self, test_settings):

        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = f"{name} description"

        captured: dict[str, list] = {}
        mock_response = _make_ai_message("No tools needed.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        def _bind_spy(llm, tools, provider):
            captured["tools"] = list(tools)
            return llm

        state = _make_state(metadata={"_usage": {}})
        context = AgentRunContext([_Tool("org_custom_tool")], {})
        with (
            agent_run_context(context),
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes.create_llm_from_config", return_value=mock_llm),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._bind_tools_for_provider", side_effect=_bind_spy),
            patch.object(nodes_module, "_cached_tools", [_Tool("calculate")]),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        bound_names = [t.name for t in captured["tools"]]
        assert "calculate" in bound_names
        assert "org_custom_tool" in bound_names

    async def test_planner_node_ignores_tools_in_checkpoint_metadata(self, test_settings):

        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = f"{name} description"

        captured: dict[str, list] = {}
        mock_response = _make_ai_message("No tools needed.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        def _bind_spy(llm, tools, provider):
            captured["tools"] = list(tools)
            return llm

        state = _make_state(metadata={"_usage": {}, "_org_tools": [_Tool("meta_org_tool")]})
        with (
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes.create_llm_from_config", return_value=mock_llm),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._bind_tools_for_provider", side_effect=_bind_spy),
            patch.object(nodes_module, "_cached_tools", [_Tool("calculate")]),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        bound_names = [t.name for t in captured["tools"]]
        assert "calculate" in bound_names
        assert "meta_org_tool" not in bound_names

    async def test_planner_node_without_org_tools(self, test_settings):
        mock_response = _make_ai_message("OK.", tool_calls=[])
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        state = _make_state(metadata={"_usage": {}})
        with (
            patch("agent.nodes.get_llm_for_request", return_value=mock_llm),
            patch("agent.nodes.create_llm_from_config", return_value=mock_llm),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._bind_tools_for_provider", side_effect=lambda llm, tools, p: llm),
            patch.object(nodes_module, "_cached_tools", []),
            patch.object(nodes_module, "_cached_tools_by_name", {}),
        ):
            result = await planner_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI


class TestToolExecutorNodeRunContextTools:
    async def test_tool_executor_org_tools_from_run_context(self, test_settings):
        call = {"id": "c1", "name": "org_custom_tool", "args": {"x": 1}}
        last_msg = _make_ai_message(tool_calls=[call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "ok"})

        state = _make_state(
            messages=[last_msg],
            metadata={"_usage": {}},
        )
        context = AgentRunContext([], {"org_custom_tool": mock_tool})
        with agent_run_context(context), patch.object(nodes_module, "_cached_tools_by_name", {}):
            result = await tool_executor_node(state)

        mock_tool.ainvoke.assert_called_once()
        assert result["task_status"] == TaskStatus.PLANNING

    async def test_tool_executor_ignores_tools_in_checkpoint_metadata(self, test_settings):
        call = {"id": "c1", "name": "org_custom_tool", "args": {"x": 1}}
        last_msg = _make_ai_message(tool_calls=[call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "ok"})

        state = _make_state(
            messages=[last_msg],
            metadata={"_usage": {}, "_org_tools_by_name": {"org_custom_tool": mock_tool}},
        )
        with patch.object(nodes_module, "_cached_tools_by_name", {}):
            result = await tool_executor_node(state)

        mock_tool.ainvoke.assert_not_called()
        assert result["metadata"]["_synthesis_forced"] is True
        assert "TOOL_UNAVAILABLE" in result["messages"][0].content

    async def test_tool_executor_without_org_tools(self, test_settings):
        call = {"id": "c1", "name": "calculate", "args": {"expression": "2+2"}}
        last_msg = _make_ai_message(tool_calls=[call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": 4})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"calculate": mock_tool}):
            result = await tool_executor_node(state)

        mock_tool.ainvoke.assert_called_once()
        assert result["task_status"] == TaskStatus.PLANNING


# ─── _resolve_planner_llm (provider routing seam) ─────────────────────────────


class TestResolvePlannerLlm:
    """Direct unit tests for the extracted planner LLM routing seam.

    Covers the cooldown -> fallback and synthesis-override branches that were
    previously inlined inside planner_node, asserting provider/model selection
    without invoking a full planner turn.
    """

    def test_cooldown_routes_to_fallback(self, test_settings):
        from agent.nodes import _resolve_planner_llm

        fallback_llm = MagicMock()
        state = _make_state()
        with (
            patch("agent.nodes.is_provider_cooled_down", return_value=True),
            patch("agent.nodes._get_fallback_llm", return_value=(fallback_llm, "anthropic", "claude-sonnet-4-6")),
            patch("agent.nodes._bind_tools_for_provider", side_effect=lambda llm, tools, provider: llm),
        ):
            llm, provider, model, _max_tokens, _timeout, fast_reason = _resolve_planner_llm(
                state,
                [],
                test_settings,
                llm_config=None,
                tool_iterations=0,
            )

        assert provider == "anthropic"
        assert model == "claude-sonnet-4-6"
        assert llm is fallback_llm
        assert fast_reason is None

    def test_byok_config_skips_fallback_and_overrides(self, test_settings):
        """BYOK (_llm_config) must be respected: no cooldown fallback, no override."""
        from agent.nodes import _resolve_planner_llm

        byok_llm = MagicMock()
        byok_cfg = {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "is_byok": True}
        state = _make_state(metadata={"_usage": {}, "_llm_config": byok_cfg})
        with (
            patch("agent.nodes.is_provider_cooled_down", return_value=True),
            patch("agent.nodes._get_fallback_llm", return_value=(MagicMock(), "anthropic", "x")) as mock_fallback,
            patch("agent.nodes.get_llm_for_request", return_value=byok_llm),
            patch("agent.nodes._bind_tools_for_provider", side_effect=lambda llm, tools, provider: llm),
        ):
            llm, provider, model, _max_tokens, _timeout, _fast_reason = _resolve_planner_llm(
                state,
                [],
                test_settings,
                llm_config=byok_cfg,
                tool_iterations=0,
            )

        mock_fallback.assert_not_called()
        assert provider == "openrouter"
        assert model == "deepseek/deepseek-v4-flash"
        assert llm is byok_llm

    def test_synthesis_override_selects_override_model_and_skips_bind(self, test_settings):
        from agent.nodes import _resolve_planner_llm

        synthesis_llm = MagicMock()
        state = _make_state(metadata={"_usage": {"tool_iterations": 1}, "_synthesis_forced": True})
        with (
            patch.object(test_settings, "agent_synthesis_provider", "openai"),
            patch.object(test_settings, "agent_synthesis_model", "gpt-4o-mini"),
            patch("agent.nodes._synthesis_fast_path_reason", return_value="forced"),
            patch("agent.nodes.get_llm_for_request", side_effect=[MagicMock(), synthesis_llm]),
            patch("agent.nodes.is_provider_cooled_down", return_value=False),
            patch("agent.nodes._provider_api_key", return_value="test-key"),
            patch("agent.nodes._bind_tools_for_provider", side_effect=lambda llm, tools, provider: llm),
        ):
            llm, provider, model, max_tokens, _timeout, fast_reason = _resolve_planner_llm(
                state,
                [],
                test_settings,
                llm_config=None,
                tool_iterations=1,
            )

        assert provider == "openai"
        assert model == "gpt-4o-mini"
        assert max_tokens == test_settings.agent_synthesis_max_tokens
        # Synthesis fast path returns the unbound LLM so tool schemas aren't sent.
        assert llm is synthesis_llm
        assert fast_reason == "forced"

    def test_fallback_model_uses_active_turn_token_budget(self, test_settings):
        from agent._provider import _get_fallback_llm

        fallback_llm = MagicMock()
        create_llm = MagicMock(return_value=fallback_llm)

        result = _get_fallback_llm(
            failed_provider="unavailable",
            failed_model="unavailable",
            settings=test_settings,
            create_llm_from_config=create_llm,
            is_provider_cooled_down=lambda _provider, _model: False,
            provider_api_key=lambda _settings, _provider: "test-key",
            max_tokens=8192,
        )

        assert result is not None
        assert create_llm.call_args.args[0]["max_tokens"] == 8192


# ─── tool_executor_node ───────────────────────────────────────────────────────


class TestToolExecutorNode:
    async def test_excluded_tool_not_executed(self, test_settings):
        call = {"id": "c1", "name": "acme/weather", "args": {"city": "NYC"}}
        last_msg = _make_ai_message(tool_calls=[call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"temp": 72})

        state = _make_state(
            messages=[last_msg],
            metadata={
                "_usage": {},
                "_org_tools_by_name": {"acme/weather": mock_tool},
                "_excluded_tool_names": ["acme/weather"],
            },
        )
        with patch.object(nodes_module, "_cached_tools_by_name", {}):
            result = await tool_executor_node(state)

        mock_tool.ainvoke.assert_not_called()
        assert result["task_status"] == TaskStatus.PLANNING
        assert len(result["messages"]) == 1
        assert "TOOL_UNAVAILABLE" in result["messages"][0].content

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

    async def test_truncated_protocol_batch_preserves_full_slots(self, test_settings):
        test_settings.agent_max_tool_result_chars = 100
        tool_call = {"id": "call-1", "name": "get_protocol_tvl", "args": {"protocols": ["liquity"]}}
        last_msg = _make_ai_message(tool_calls=[tool_call])
        payload = [
            {
                "protocol": "liquity",
                "current_tvl_usd": 1000.0,
                "tvl_7d_change_pct": 1.0,
                "tvl_30d_change_pct": 2.0,
                "current_fees_usd": 300.0,
                "fees_7d_change_pct": -25.0,
                "fees_30d_change_pct": 200.0,
                "current_revenue_usd": None,
                "revenue_7d_change_pct": None,
                "revenue_30d_change_pct": None,
                "revenue_error_type": "upstream_error",
                "chain_breakdown": [],
                "historical_series": None,
                "note": "ok",
            }
        ]
        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value=payload)

        state = _make_state(messages=[last_msg])
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_protocol_tvl": mock_tool}):
            result = await tool_executor_node(state)

        assert "TRUNCATED" in result["messages"][0].content
        assert result["slots"]["tvl"]["liquity"]["current_fees_usd"] == 300.0
        assert result["slots"]["tvl"]["liquity"]["revenue_error_type"] == "upstream_error"

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
        # First message (skipped_messages) is an error for the missing tool
        # The pre-check now catches missing tools before they reach execution.
        content = result["messages"][0].content
        assert "TOOL_UNAVAILABLE" in content

    async def test_tool_call_count_accumulated_in_usage(self, test_settings):
        calls = [
            {"id": "c1", "name": "get_datetime", "args": {"tz": "UTC"}},
            {"id": "c2", "name": "get_datetime", "args": {"tz": "PST"}},
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

    async def test_duplicate_platform_call_is_not_counted(self, test_settings):
        calls = [
            {"id": "c1", "name": "get_datetime", "args": {}},
            {"id": "c2", "name": "get_datetime", "args": {}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "now"})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_datetime": mock_tool}):
            result = await tool_executor_node(state)

        usage = result["metadata"]["_usage"]
        assert usage["tool_calls"] == 1
        assert mock_tool.ainvoke.call_count == 1

    async def test_tool_call_log_records_success_and_failure(self, test_settings):
        """_tool_call_log is the per-call telemetry accumulator persisted after
        the run by teardrop.usage.record_tool_call_events (ML/reputation data).

        Note: an *unregistered* tool name never reaches execution -- it is
        pre-empted into a TOOL_UNAVAILABLE skipped_message before dedup_calls
        is built (see the "tool_unavailable" pre-check above), so it never
        appears in _tool_call_log. To exercise a genuine failure entry we use
        a registered tool whose ainvoke() raises.
        """
        calls = [
            {"id": "c1", "name": "calculate", "args": {"expression": "2+2"}},
            {"id": "c2", "name": "failing_tool", "args": {}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_calc = MagicMock()
        mock_calc.ainvoke = AsyncMock(return_value={"result": 4.0})
        mock_failing = MagicMock()
        mock_failing.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

        state = _make_state(messages=[last_msg])
        with patch.object(nodes_module, "_cached_tools_by_name", {"calculate": mock_calc, "failing_tool": mock_failing}):
            result = await tool_executor_node(state)

        tool_call_log = result["metadata"]["_usage"]["_tool_call_log"]
        assert len(tool_call_log) == 2

        by_name = {entry["tool_name"]: entry for entry in tool_call_log}
        assert by_name["calculate"]["success"] is True
        assert by_name["calculate"]["error_class"] == ""
        assert by_name["calculate"]["billable"] is True
        assert isinstance(by_name["calculate"]["elapsed_ms"], int)
        assert by_name["calculate"]["args_hash"]  # non-empty hash, not raw args

        assert by_name["failing_tool"]["success"] is False
        assert by_name["failing_tool"]["error_class"]
        assert by_name["failing_tool"]["billable"] is False

    async def test_tool_call_log_accumulates_across_iterations(self, test_settings):
        call = {"id": "c1", "name": "calculate", "args": {"expression": "1+1"}}
        last_msg = _make_ai_message(tool_calls=[call])

        mock_calc = MagicMock()
        mock_calc.ainvoke = AsyncMock(return_value={"result": 2.0})

        existing_entry = {
            "tool_name": "get_datetime",
            "success": True,
            "error_class": "",
            "elapsed_ms": 5,
            "billable": True,
            "args_hash": "priorhash",
        }
        state = _make_state(
            messages=[last_msg],
            metadata={"_usage": {"_tool_call_log": [existing_entry]}},
        )
        with patch.object(nodes_module, "_cached_tools_by_name", {"calculate": mock_calc}):
            result = await tool_executor_node(state)

        tool_call_log = result["metadata"]["_usage"]["_tool_call_log"]
        assert len(tool_call_log) == 2
        assert tool_call_log[0] == existing_entry
        assert tool_call_log[1]["tool_name"] == "calculate"

    async def test_org_tool_is_not_deduplicated(self, test_settings):
        calls = [
            {"id": "c1", "name": "org__send_invoice", "args": {"invoice_id": "inv-1"}},
            {"id": "c2", "name": "org__send_invoice", "args": {"invoice_id": "inv-1"}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"status": "sent"})

        state = _make_state(
            messages=[last_msg],
            metadata={"_usage": {}},
        )
        context = AgentRunContext([], {"org__send_invoice": mock_tool})
        with agent_run_context(context), patch.object(nodes_module, "_cached_tools_by_name", {}):
            result = await tool_executor_node(state)

        usage = result["metadata"]["_usage"]
        assert usage["tool_calls"] == 2
        assert mock_tool.ainvoke.call_count == 2

    async def test_no_tool_calls_returns_generating_ui(self, test_settings):
        """If the last message has no tool_calls the node skips to ui_generator."""
        last_msg = _make_ai_message(tool_calls=[])
        state = _make_state(messages=[last_msg])

        result = await tool_executor_node(state)

        assert result["task_status"] == TaskStatus.GENERATING_UI
        assert "messages" not in result

    async def test_tool_iterations_incremented(self, test_settings):
        """Each tool_executor_node call increments tool_iterations by exactly 1."""
        tool_call = {"id": "c1", "name": "get_datetime", "args": {}}
        last_msg = _make_ai_message(tool_calls=[tool_call])
        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "now"})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_datetime": mock_tool}):
            result = await tool_executor_node(state)

        assert result["metadata"]["_usage"]["tool_iterations"] == 1

    async def test_tool_iterations_accumulates_across_calls(self, test_settings):
        """tool_iterations adds on top of the existing value from prior cycles."""
        tool_call = {"id": "c1", "name": "get_datetime", "args": {}}
        last_msg = _make_ai_message(tool_calls=[tool_call])
        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "now"})

        state = _make_state(messages=[last_msg], metadata={"_usage": {"tool_iterations": 2}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_datetime": mock_tool}):
            result = await tool_executor_node(state)

        assert result["metadata"]["_usage"]["tool_iterations"] == 3

    async def test_tool_executor_timeout_returns_failed_status(self, test_settings):
        """If a tool hangs, it is converted to timeout ToolMessage and synthesis continues."""
        tool_call = {"id": "c1", "name": "slow_tool", "args": {}}
        last_msg = _make_ai_message(tool_calls=[tool_call])

        # A tool that sleeps forever
        async def slow_invoke(*args, **kwargs):
            try:
                import asyncio as _asyncio

                await _asyncio.sleep(10)
            except ImportError:
                import asyncio

                await asyncio.sleep(10)
            return {"result": "finally done"}

        mock_tool = MagicMock()
        mock_tool.ainvoke = slow_invoke

        state = _make_state(messages=[last_msg])

        with patch.object(nodes_module, "_get_cached_tools_by_name", return_value={"slow_tool": mock_tool}):
            with patch.object(test_settings, "agent_single_tool_timeout_seconds", 0.05):
                result = await tool_executor_node(state)

        assert result["task_status"] == TaskStatus.PLANNING
        assert "[TOOL_TIMEOUT]" in result["messages"][0].content
        assert len(result["messages"]) == 1

    async def test_partial_tool_batch_one_timeout_routes_to_planning(self, test_settings):
        calls = [
            {"id": "c1", "name": "fast_tool", "args": {}},
            {"id": "c2", "name": "slow_tool", "args": {}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        fast_tool = MagicMock()
        fast_tool.ainvoke = AsyncMock(return_value={"result": "ok"})

        async def slow_invoke(*args, **kwargs):
            import asyncio

            await asyncio.sleep(10)
            return {"result": "late"}

        slow_tool = MagicMock()
        slow_tool.ainvoke = slow_invoke

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        tool_map = {"fast_tool": fast_tool, "slow_tool": slow_tool}
        with patch.object(nodes_module, "_get_cached_tools_by_name", return_value=tool_map):
            with patch.object(test_settings, "agent_single_tool_timeout_seconds", 0.05):
                result = await tool_executor_node(state)

        assert result["task_status"] == TaskStatus.PLANNING
        assert len(result["messages"]) == 2
        assert any("[TOOL_TIMEOUT]" in m.content for m in result["messages"])
        assert result["metadata"]["_usage"]["failed_tool_calls"] == 1

    async def test_get_token_price_all_address_tokens_blocked(self, test_settings):
        call = {"id": "c1", "name": "get_token_price", "args": {"tokens": ["0xabc123", "0xdef456"]}}
        last_msg = _make_ai_message(tool_calls=[call])
        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"prices": []})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_token_price": mock_tool}):
            result = await tool_executor_node(state)

        mock_tool.ainvoke.assert_not_called()
        assert "GET_TOKEN_PRICE_BLOCKED" in result["messages"][0].content

    async def test_get_token_price_mixed_tokens_not_blocked(self, test_settings):
        call = {"id": "c1", "name": "get_token_price", "args": {"tokens": ["ETH", "0xabc123"]}}
        last_msg = _make_ai_message(tool_calls=[call])
        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"prices": [{"symbol": "ETH", "price": 3000}]})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_token_price": mock_tool}):
            result = await tool_executor_node(state)

        assert result["task_status"] == TaskStatus.PLANNING
        mock_tool.ainvoke.assert_called_once()

    async def test_unbound_tool_call_returns_unavailable_message(self, test_settings):
        """A tool call for an unbound tool must produce [TOOL_UNAVAILABLE]."""
        call = {"id": "c1", "name": "unknown_tool", "args": {}}
        last_msg = _make_ai_message(tool_calls=[call])

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {}):
            result = await tool_executor_node(state)

        assert result["task_status"] == TaskStatus.PLANNING
        assert len(result["messages"]) == 1
        assert "[TOOL_UNAVAILABLE]" in result["messages"][0].content
        assert "unknown_tool" in result["messages"][0].content

    async def test_all_calls_suppressed_routes_back_to_planning(self, test_settings):
        """If every tool call is suppressed, force a synthesis planner turn instead of ending blank."""
        tool_call = {"id": "c1", "name": "get_datetime", "args": {}}
        last_msg = _make_ai_message(tool_calls=[tool_call])

        from agent.nodes import _call_signature

        prior_sig = _call_signature("get_datetime", {})
        state = _make_state(
            messages=[last_msg],
            metadata={"_usage": {"_completed_calls": [prior_sig], "tool_iterations": 2}},
        )
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_datetime": MagicMock()}):
            result = await tool_executor_node(state)

        assert result["task_status"] == TaskStatus.PLANNING
        assert result["metadata"]["_synthesis_forced"] is True
        assert result["metadata"]["_usage"]["tool_iterations"] == 3


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

    async def test_emit_ui_false_skips_llm_generation(self, test_settings):
        last_msg = _make_ai_message(content="Portfolio value is 123.45 USD")
        state = _make_state(messages=[last_msg], metadata={"_usage": {}, "emit_ui": False})

        with patch("agent.node_ui.get_llm_for_request") as mock_get_llm:
            result = await ui_generator_node(state)

        assert result["task_status"] == TaskStatus.COMPLETED
        assert result.get("ui_components", []) == []
        mock_get_llm.assert_not_called()

    async def test_emit_ui_true_uses_speed_tier_when_no_org_config(self, test_settings):
        last_msg = _make_ai_message(content="APR is 3.14%")
        state = _make_state(messages=[last_msg], metadata={"_usage": {}, "emit_ui": True})

        mock_ui_llm = MagicMock()
        mock_ui_llm.ainvoke = AsyncMock(return_value=_make_ai_message(content='{"components": []}'))

        with (
            patch("agent.node_ui.create_llm_from_config", return_value=mock_ui_llm) as mock_create,
            patch("agent.node_ui._contains_structured_data", return_value=True),
        ):
            result = await ui_generator_node(state)

        assert result["task_status"] == TaskStatus.COMPLETED
        cfg = mock_create.call_args.args[0]
        assert cfg["provider"] == test_settings.agent_ui_generator_provider
        assert cfg["model"] == test_settings.agent_ui_generator_model

    async def test_emit_ui_true_uses_org_llm_config_when_present(self, test_settings):
        last_msg = _make_ai_message(content="TVL is 1.23B")
        state = _make_state(
            messages=[last_msg],
            metadata={
                "_usage": {},
                "emit_ui": True,
                "_llm_config": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"},
            },
        )

        mock_byok_llm = MagicMock()
        mock_byok_llm.ainvoke = AsyncMock(return_value=_make_ai_message(content='{"components": []}'))

        with (
            patch("agent.node_ui.get_llm_for_request", return_value=mock_byok_llm) as mock_get,
            patch("agent.node_ui.create_llm_from_config") as mock_create,
            patch("agent.node_ui._contains_structured_data", return_value=True),
        ):
            result = await ui_generator_node(state)

        assert result["task_status"] == TaskStatus.COMPLETED
        mock_get.assert_called_once()
        mock_create.assert_not_called()

    async def test_malformed_a2ui_json_returns_no_components(self, test_settings):
        text = "```a2ui\n{bad json}\n```"
        last_msg = _make_ai_message(content=text)
        state = _make_state(messages=[last_msg])

        result = await ui_generator_node(state)

        # task_status should still be COMPLETED; ui_components absent or empty
        assert result["task_status"] == TaskStatus.COMPLETED
        assert result.get("ui_components", []) == []

    async def test_falls_back_to_latest_ai_message_when_last_is_tool_message(self, test_settings):
        ai_msg = _make_ai_message(content='```a2ui\n{"components": [{"type": "text", "props": {"content": "Done"}}]}\n```')
        tool_msg = ToolMessage(content="tool output", tool_call_id="t-1")
        state = _make_state(messages=[HumanMessage(content="run"), ai_msg, tool_msg])

        result = await ui_generator_node(state)

        assert result["task_status"] == TaskStatus.COMPLETED
        assert len(result["ui_components"]) == 1


# ─── _call_signature ─────────────────────────────────────────────────────────


class TestCallSignature:
    def test_same_name_same_args_produces_same_sig(self):
        from agent.nodes import _call_signature

        a = _call_signature("get_wallet_portfolio", {"wallet_address": "0xabc", "chain_id": 1})
        b = _call_signature("get_wallet_portfolio", {"wallet_address": "0xabc", "chain_id": 1})
        assert a == b

    def test_different_tool_name_produces_different_sig(self):
        from agent.nodes import _call_signature

        a = _call_signature("get_wallet_portfolio", {"chain_id": 1})
        b = _call_signature("get_defi_positions", {"chain_id": 1})
        assert a != b

    def test_different_args_produces_different_sig(self):
        from agent.nodes import _call_signature

        a = _call_signature("get_defi_positions", {"chain_id": 1})
        b = _call_signature("get_defi_positions", {"chain_id": 8453})
        assert a != b

    def test_arg_key_order_does_not_affect_sig(self):
        from agent.nodes import _call_signature

        a = _call_signature("tool", {"a": 1, "b": 2})
        b = _call_signature("tool", {"b": 2, "a": 1})
        assert a == b

    def test_sig_has_expected_format(self):
        from agent.nodes import _call_signature

        sig = _call_signature("my_tool", {"x": 1})
        assert sig.startswith("my_tool:")
        assert len(sig) == len("my_tool:") + 16


# ─── tool_executor_node — deduplication ──────────────────────────────────────


class TestToolExecutorDedup:
    async def test_duplicate_call_same_args_is_blocked(self, test_settings):
        """Second identical call returns DUPLICATE_CALL_BLOCKED without invoking the tool."""
        tool_call = {"id": "call-1", "name": "get_datetime", "args": {}}
        last_msg = _make_ai_message(tool_calls=[tool_call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "now"})

        from agent.nodes import _call_signature

        prior_sig = _call_signature("get_datetime", {})
        state = _make_state(
            messages=[last_msg],
            metadata={"_usage": {"_completed_calls": [prior_sig]}},
        )
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_datetime": mock_tool}):
            result = await tool_executor_node(state)

        # Tool should NOT have been invoked
        mock_tool.ainvoke.assert_not_called()
        assert len(result["messages"]) == 1
        assert "DUPLICATE_CALL_BLOCKED" in result["messages"][0].content

    async def test_same_tool_different_args_not_blocked(self, test_settings):
        """get_defi_positions(chain_id=1) and (chain_id=8453) are distinct — both must run."""
        calls = [
            {"id": "c1", "name": "get_defi_positions", "args": {"chain_id": 1}},
            {"id": "c2", "name": "get_defi_positions", "args": {"chain_id": 8453}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"positions": []})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_defi_positions": mock_tool}):
            result = await tool_executor_node(state)

        assert mock_tool.ainvoke.call_count == 2
        assert len(result["messages"]) == 2
        assert not any("DUPLICATE_CALL_BLOCKED" in m.content for m in result["messages"])

    async def test_dedup_sigs_persisted_in_state(self, test_settings):
        """After execution, _completed_calls contains the new signature."""
        tool_call = {"id": "c1", "name": "get_datetime", "args": {}}
        last_msg = _make_ai_message(tool_calls=[tool_call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "now"})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_datetime": mock_tool}):
            result = await tool_executor_node(state)

        from agent.nodes import _call_signature

        expected_sig = _call_signature("get_datetime", {})
        assert expected_sig in result["metadata"]["_usage"]["_completed_calls"]

    async def test_completed_calls_absent_does_not_raise(self, test_settings):
        """No _completed_calls key in state must not raise — initialises empty."""
        tool_call = {"id": "c1", "name": "get_datetime", "args": {}}
        last_msg = _make_ai_message(tool_calls=[tool_call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"result": "now"})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_datetime": mock_tool}):
            result = await tool_executor_node(state)

        assert result["task_status"] == TaskStatus.PLANNING
        assert "_completed_calls" in result["metadata"]["_usage"]

    async def test_duplicate_within_same_batch_blocked(self, test_settings):
        """Two identical calls in the same AI message — only the first executes."""
        identical_args = {"wallet_address": "0xabc", "chain_id": 1}
        calls = [
            {"id": "c1", "name": "get_wallet_portfolio", "args": identical_args},
            {"id": "c2", "name": "get_wallet_portfolio", "args": identical_args},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"holdings": []})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_wallet_portfolio": mock_tool}):
            result = await tool_executor_node(state)

        # Only one real invocation
        assert mock_tool.ainvoke.call_count == 1
        # Two messages returned: one real + one DUPLICATE_CALL_BLOCKED
        assert len(result["messages"]) == 2
        blocked = [m for m in result["messages"] if "DUPLICATE_CALL_BLOCKED" in m.content]
        assert len(blocked) == 1

    async def test_tool_name_cap_blocks_second_call_even_with_different_args(self, test_settings):
        """Per-tool cap should block a second call regardless of argument changes."""
        calls = [
            {"id": "c1", "name": "get_yield_rates", "args": {"chain": "Ethereum"}},
            {"id": "c2", "name": "get_yield_rates", "args": {"chain": "Base"}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"pools": []})

        state = _make_state(messages=[last_msg], metadata={"_usage": {}})
        with (
            patch.object(nodes_module, "_cached_tools_by_name", {"get_yield_rates": mock_tool}),
            patch.object(test_settings, "agent_tool_max_calls_per_run", {"get_yield_rates": 1}),
        ):
            result = await tool_executor_node(state)

        assert mock_tool.ainvoke.call_count == 1
        blocked = [m for m in result["messages"] if "TOOL_CALL_CAP_EXCEEDED" in m.content]
        assert len(blocked) == 1
        assert result["metadata"]["_usage"]["_tool_call_counts"]["get_yield_rates"] == 1

    async def test_liquidation_risk_blocked_when_defi_positions_already_covered(self, test_settings):
        """If get_defi_positions already covered wallet+chain, liquidation risk is suppressed."""
        call = {
            "id": "c1",
            "name": "get_liquidation_risk",
            "args": {
                "wallet_addresses": ["0xd8da6bf26964af9d7eed9e03e53415d37aa96045"],
                "chain_id": 1,
            },
        }
        last_msg = _make_ai_message(tool_calls=[call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"results": []})

        state = _make_state(
            messages=[last_msg],
            metadata={
                "_usage": {
                    "_defi_positions_covered": ["1:0xd8da6bf26964af9d7eed9e03e53415d37aa96045"],
                }
            },
        )
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_liquidation_risk": mock_tool}):
            result = await tool_executor_node(state)

        mock_tool.ainvoke.assert_not_called()
        blocked = [m for m in result["messages"] if "SEMANTIC_REDUNDANCY_BLOCKED" in m.content]
        assert len(blocked) == 1

    async def test_liquidation_risk_not_blocked_when_defi_positions_not_covered(self, test_settings):
        """Guard should allow get_liquidation_risk when coverage data is absent."""
        call = {
            "id": "c1",
            "name": "get_liquidation_risk",
            "args": {
                "wallet_addresses": ["0xd8da6bf26964af9d7eed9e03e53415d37aa96045"],
                "chain_id": 1,
            },
        }
        last_msg = _make_ai_message(tool_calls=[call])

        mock_tool = MagicMock()
        mock_tool.ainvoke = AsyncMock(return_value={"results": []})

        state = _make_state(messages=[last_msg], metadata={"_usage": {"_defi_positions_covered": []}})
        with patch.object(nodes_module, "_cached_tools_by_name", {"get_liquidation_risk": mock_tool}):
            await tool_executor_node(state)

        mock_tool.ainvoke.assert_called_once()

    async def test_custom_tool_cap_blocks_non_platform_calls(self, test_settings):
        calls = [
            {"id": "c1", "name": "org__tool_a", "args": {"x": 1}},
            {"id": "c2", "name": "org__tool_b", "args": {"x": 2}},
        ]
        last_msg = _make_ai_message(tool_calls=calls)

        mock_tool_a = MagicMock()
        mock_tool_a.ainvoke = AsyncMock(return_value={"ok": True})
        mock_tool_b = MagicMock()
        mock_tool_b.ainvoke = AsyncMock(return_value={"ok": True})

        state = _make_state(
            messages=[last_msg],
            metadata={"_usage": {"custom_tool_calls": 0}},
        )

        context = AgentRunContext([], {"org__tool_a": mock_tool_a, "org__tool_b": mock_tool_b})
        with agent_run_context(context), patch.object(test_settings, "max_custom_tool_calls_per_run", 1):
            result = await tool_executor_node(state)

        assert mock_tool_a.ainvoke.call_count + mock_tool_b.ainvoke.call_count == 1
        blocked = [m for m in result["messages"] if "CUSTOM_TOOL_CAP_EXCEEDED" in m.content]
        assert len(blocked) == 1
        assert result["metadata"]["_usage"]["custom_tool_calls"] == 1


# ─── Serialization guard ─────────────────────────────────────────────────────


class TestAgentStateMetadata:
    """AgentState model_dump must not fail when tool containers are empty."""

    def test_metadata_serializable_with_empty_tool_containers(self):
        """AgentState with empty _org_tools and _org_tools_by_name JSON-serializes cleanly."""
        import json

        state = AgentState(
            messages=[HumanMessage(content="test")],
            metadata={
                "_org_tools": [],
                "_org_tools_by_name": {},
                "_usage": {"tool_iterations": 0},
                "_excluded_tool_names": [],
            },
        )
        dumped = state.model_dump()
        serialized = json.dumps(dumped["metadata"])
        assert '"tool_iterations"' in serialized
