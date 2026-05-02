"""Unit tests for tools.executor shared execution helper."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from pydantic import BaseModel, Field, StrictBool

from tools.executor import execute_tool


class _DummyTool:
    def __init__(self, result=None, exc: Exception | None = None, metadata: dict | None = None):
        self._result = result
        self._exc = exc
        self.metadata = metadata or {}

    async def ainvoke(self, _args):
        if self._exc is not None:
            raise self._exc
        return self._result


class _OutputModel(BaseModel):
    ok: StrictBool = Field(...)


@pytest.mark.anyio
async def test_execute_tool_success_dict_result():
    tool = _DummyTool(result={"ok": True})
    res = await execute_tool(
        tool_name="t",
        tool_call_id="c1",
        tool_args={"a": 1},
        tool=tool,
    )
    assert res.success is True
    assert res.billable is True
    assert res.error_class is None
    assert res.content == '{"ok": true}'


@pytest.mark.anyio
async def test_execute_tool_timeout_classified_non_billable():
    async def _slow(_args):
        await asyncio.sleep(0.05)
        return {"ok": True}

    class _SlowTool:
        metadata = {"timeout_seconds": 0.001}

        async def ainvoke(self, args):
            return await _slow(args)

    res = await execute_tool(
        tool_name="slow",
        tool_call_id="c2",
        tool_args={},
        tool=_SlowTool(),
    )
    assert res.success is False
    assert res.error_class == "timeout"
    assert res.billable is False
    assert res.retry_safe is True


@pytest.mark.anyio
async def test_execute_tool_validation_error_classified_non_billable():
    class _Req(BaseModel):
        foo: int

    class _SchemaTool:
        async def ainvoke(self, _args):
            _Req.model_validate({})
            return {"ok": True}

    res = await execute_tool(
        tool_name="validate",
        tool_call_id="c3",
        tool_args={},
        tool=_SchemaTool(),
    )
    assert res.success is False
    assert res.error_class == "validation_error"
    assert res.billable is False


@pytest.mark.anyio
async def test_execute_tool_embedded_upstream_error_non_billable():
    tool = _DummyTool(result={"error": "Webhook request failed: ClientConnectorError"})
    res = await execute_tool(
        tool_name="webhook",
        tool_call_id="c4",
        tool_args={},
        tool=tool,
    )
    assert res.success is False
    assert res.error_class == "upstream_unavailable"
    assert res.billable is False
    assert res.retry_safe is True


@pytest.mark.anyio
async def test_execute_tool_embedded_business_error_billable():
    tool = _DummyTool(result={"error": "Portfolio unavailable for this wallet"})
    res = await execute_tool(
        tool_name="biz",
        tool_call_id="c5",
        tool_args={},
        tool=tool,
    )
    assert res.success is False
    assert res.error_class == "business_error"
    assert res.billable is True


@pytest.mark.anyio
async def test_execute_tool_output_contract_error_non_billable():
    tool = _DummyTool(result={"ok": "yes"}, metadata={"output_schema": _OutputModel})
    res = await execute_tool(
        tool_name="out",
        tool_call_id="c6",
        tool_args={},
        tool=tool,
    )
    assert res.success is False
    assert res.error_class == "output_contract_error"
    assert res.billable is False


@pytest.mark.anyio
async def test_execute_tool_non_serializable_coerces_to_string():
    class _Thing:
        def __str__(self):
            return "x"

    tool = _DummyTool(result={"value": _Thing()})
    res = await execute_tool(
        tool_name="ser",
        tool_call_id="c7",
        tool_args={},
        tool=tool,
    )
    assert res.success is True
    assert '"value": "x"' in res.content


@pytest.mark.anyio
async def test_execute_tool_aiohttp_error_classified_non_billable():
    exc = aiohttp.ClientError("nope")
    tool = _DummyTool(exc=exc)
    res = await execute_tool(
        tool_name="net",
        tool_call_id="c8",
        tool_args={},
        tool=tool,
    )
    assert res.success is False
    assert res.error_class == "upstream_unavailable"
    assert res.billable is False
