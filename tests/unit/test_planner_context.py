# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for runtime context injection in planner_node.

Verifies that the system prompt passed to the LLM always contains:
  - Current date/time (not training-data fallback)
  - Model identity and knowledge cutoff
  - Context window and token limits
  - Org name and user role (when provided)
  - Credit balance (when provided)
  - Available tools summary

No real LLM calls; the ainvoke is mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from agent.nodes import planner_node
from agent.state import AgentState

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_state(**metadata_overrides) -> AgentState:
    metadata = {
        "_usage": {"tokens_in": 0, "tokens_out": 0, "tool_calls": 0, "tool_names": []},
        "_org_tools": [],
        "_memories": [],
        "_llm_config": None,
        "_org_name": "",
        "_user_role": "user",
        "_user_wallet_address": None,
        "_credit_balance_usdc": None,
    }
    metadata.update(metadata_overrides)
    return AgentState(
        messages=[HumanMessage(content="What is the current BTC price?")],
        metadata=metadata,
    )


def _make_ai_response(content: str = "The current BTC price is...") -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = []
    msg.usage_metadata = {"input_tokens": 100, "output_tokens": 50}
    return msg


def _captured_system_prompt(mock_ainvoke: MagicMock) -> str:
    """Extract the concatenated system prompt from the messages passed to ainvoke."""
    call_args = mock_ainvoke.call_args
    messages = call_args[0][0]  # positional arg: list of messages
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    assert system_msgs, "No SystemMessage found in LLM call"
    return "\n\n".join(m.content for m in system_msgs)


def _default_settings():
    return MagicMock(
        agent_provider="anthropic",
        agent_model="claude-haiku-4-5-20251001",
        agent_max_tokens=4096,
        agent_llm_timeout_seconds=120,
    )


# ─── Date/Time injection ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_system_prompt_contains_current_date():
    """planner_node must inject a date that matches today — not a training cutoff."""
    from datetime import datetime, timezone

    state = _make_state()
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    today_year = str(datetime.now(timezone.utc).year)
    assert today_year in system_prompt, f"Expected current year {today_year} in system prompt, got:\n{system_prompt[:500]}"
    assert "Date & Time (UTC)" in system_prompt
    assert "ISO 8601" in system_prompt


@pytest.mark.anyio
async def test_system_prompt_contains_model_knowledge_cutoff():
    """Knowledge cutoff must be injected so models don't assume training-data dates."""
    state = _make_state(
        _llm_config={
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 4096,
            "timeout_seconds": 120,
        }
    )
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "Model Knowledge Cutoff" in system_prompt
    # Claude Haiku 4.5 has a 2025-10 cutoff in the catalogue.
    assert "2025-10" in system_prompt


@pytest.mark.anyio
async def test_knowledge_cutoff_falls_back_for_unknown_model():
    """Unknown models fall back to 'Unknown' — prompt still renders without error."""
    state = _make_state(
        _llm_config={
            "provider": "openai",
            "model": "gpt-99-ultra-unknown",
            "max_tokens": 2048,
            "timeout_seconds": 60,
        }
    )
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = MagicMock(
                    agent_provider="openai",
                    agent_model="gpt-99-ultra-unknown",
                    agent_max_tokens=2048,
                    agent_llm_timeout_seconds=60,
                )
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "Model Knowledge Cutoff" in system_prompt
    assert "Unknown" in system_prompt


# ─── llm_config=None (global default path) ───────────────────────────────────


@pytest.mark.anyio
async def test_context_injected_when_llm_config_is_none():
    """When llm_config is None the global default settings are used — no KeyError."""
    state = _make_state(_llm_config=None)
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = MagicMock(
                    agent_provider="anthropic",
                    agent_model="claude-sonnet-4-20250514",
                    agent_max_tokens=8192,
                    agent_llm_timeout_seconds=180,
                )
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "anthropic/claude-sonnet-4-20250514" in system_prompt
    assert "Date & Time (UTC)" in system_prompt


# ─── Org, user role, and credit balance ──────────────────────────────────────


@pytest.mark.anyio
async def test_org_name_injected_into_system_prompt():
    state = _make_state(_org_name="Acme Trading Co", _user_role="admin")
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "Acme Trading Co" in system_prompt
    assert "admin" in system_prompt


@pytest.mark.anyio
async def test_org_name_omitted_when_empty():
    """When org_name is '' the Organisation line must not appear."""
    state = _make_state(_org_name="", _user_role="user")
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "**Organisation**" not in system_prompt
    assert "**User Role**" in system_prompt


@pytest.mark.anyio
async def test_credit_balance_injected_when_provided():
    """Credit balance (in atomic USDC) must be formatted as USD dollars."""
    # 2_430_000 atomic USDC = $2.43
    state = _make_state(_credit_balance_usdc=2430000)
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "Remaining Credit Balance" in system_prompt
    assert "$2.4300 USD" in system_prompt


@pytest.mark.anyio
async def test_credit_balance_omitted_when_none():
    state = _make_state(_credit_balance_usdc=None)
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "Remaining Credit Balance" not in system_prompt


# ─── User wallet address (SIWE) ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_wallet_address_injected_when_siwe_auth():
    """Full EIP-55 address from SIWE JWT must appear in the system prompt."""
    state = _make_state(_user_wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "User Wallet Address" in system_prompt
    assert "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045" in system_prompt


@pytest.mark.anyio
async def test_wallet_address_omitted_when_none():
    """When _user_wallet_address is None (email/client-creds) the line must not appear."""
    state = _make_state(_user_wallet_address=None)
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "User Wallet Address" not in system_prompt


# ─── Prompt injection sanitisation ───────────────────────────────────────────


@pytest.mark.anyio
async def test_org_name_backticks_sanitised():
    """Backticks in org_name must be replaced — prevents fenced block injection."""
    state = _make_state(_org_name="Evil ```injected block``` Corp")
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    runtime_section = system_prompt.split("## Current Runtime Context")[1]
    tools_split = runtime_section.split("## Available Tools")
    context_only = tools_split[0]
    assert "```" not in context_only


# ─── Available tools summary ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_available_tools_section_present_when_tools_exist():
    """Tool names must appear in system prompt when tools are bound."""
    mock_tool = MagicMock()
    mock_tool.name = "web_search"
    mock_tool.description = "Search the web for information.\nMore details follow."

    state = _make_state()
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[mock_tool]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "## Available Platform Tools" in system_prompt
    assert "web_search" in system_prompt
    # Only the first line of description should appear.
    assert "Search the web for information." in system_prompt
    assert "More details follow." not in system_prompt


@pytest.mark.anyio
async def test_available_tools_section_absent_when_no_tools():
    state = _make_state()
    ai_resp = _make_ai_response()
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=ai_resp)

    with patch("agent.nodes.get_llm_for_request") as mock_factory:
        with patch("agent.nodes.get_settings") as mock_settings:
            with patch("agent.nodes._get_cached_tools", return_value=[]):
                mock_settings.return_value = _default_settings()
                mock_factory.return_value = mock_llm
                await planner_node(state)

    system_prompt = _captured_system_prompt(mock_llm.ainvoke)
    assert "## Available Platform Tools" not in system_prompt
    assert "## Additional Organisation Tools" not in system_prompt


# ─── get_model_context_specs ─────────────────────────────────────────────────


class TestGetModelContextSpecs:
    def test_known_anthropic_haiku_returns_correct_cutoff(self):
        from benchmarks import get_model_context_specs

        specs = get_model_context_specs("anthropic", "claude-haiku-4-5-20251001")
        assert specs["knowledge_cutoff"] == "2025-10"
        assert specs["context_window"] == 200_000
        assert specs["supports_tools"] is True

    def test_known_openai_gpt4o_returns_correct_cutoff(self):
        from benchmarks import get_model_context_specs

        specs = get_model_context_specs("openai", "gpt-4o")
        assert specs["knowledge_cutoff"] == "2024-04"
        assert specs["context_window"] == 128_000

    def test_known_google_gemini_flash_returns_correct_cutoff(self):
        from benchmarks import get_model_context_specs

        specs = get_model_context_specs("google", "gemini-2.0-flash")
        assert specs["knowledge_cutoff"] == "2025-01"
        assert specs["context_window"] == 1_000_000

    def test_unknown_model_returns_safe_defaults(self):
        from benchmarks import get_model_context_specs

        specs = get_model_context_specs("openai", "gpt-99-does-not-exist")
        assert specs["knowledge_cutoff"] == "Unknown"
        assert specs["context_window"] == 128_000
        assert specs["supports_tools"] is True

    def test_returns_independent_dict_per_call(self):
        """Mutating the returned dict must not affect subsequent calls."""
        from benchmarks import get_model_context_specs

        specs1 = get_model_context_specs("openai", "gpt-99-does-not-exist")
        specs1["knowledge_cutoff"] = "mutated"
        specs2 = get_model_context_specs("openai", "gpt-99-does-not-exist")
        assert specs2["knowledge_cutoff"] == "Unknown"
