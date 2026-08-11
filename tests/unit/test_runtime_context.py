import asyncio

import pytest

from agent.runtime_context import AgentRunContext, agent_run_context, get_agent_run_context


def test_context_is_reset_after_scope():
    with agent_run_context(AgentRunContext([], {})):
        assert get_agent_run_context().org_tools == []

    with pytest.raises(RuntimeError, match="unavailable"):
        get_agent_run_context()


async def test_context_is_isolated_between_tasks():
    async def read_context(name: str) -> str:
        with agent_run_context(AgentRunContext([name], {})):
            await asyncio.sleep(0)
            return get_agent_run_context().org_tools[0]

    assert await asyncio.gather(read_context("first"), read_context("second")) == ["first", "second"]


def test_context_repr_does_not_expose_tool_data():
    context = AgentRunContext(["secret-token"], {"tool": "authorization-header"})

    assert "secret-token" not in repr(context)
    assert "authorization-header" not in repr(context)
