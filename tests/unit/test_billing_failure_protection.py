"""Unit tests for billing protection — failed tool calls must not debit credit.

Covers the two settle paths:
    - app.py mcp_jsonrpc_handler debit gate (``execution_failed`` check).
    - mcp_gateway.MCPGateway._settle_billing skips when execution_failed=True.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_gateway import MCPGatewayMiddleware


@pytest.mark.asyncio
async def test_settle_billing_skips_debit_on_failed_execution():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock(spec=[])  # no x402_billing attr
    response = MagicMock()
    pending = ("org-1", 100, "test_tool", "req-1")

    with patch("billing.debit_credit", new_callable=AsyncMock) as debit_mock, \
         patch("billing.settle_payment", new_callable=AsyncMock) as settle_mock:
        result = await gateway._settle_billing(
            request, pending, response, execution_failed=True
        )

    # Neither debit nor settle should fire.
    debit_mock.assert_not_called()
    settle_mock.assert_not_called()
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_debits_on_success():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock(spec=[])  # no x402_billing
    response = MagicMock()
    pending = ("org-1", 100, "test_tool", "req-1")  # no slash → skip earnings branch

    with patch("billing.debit_credit", new_callable=AsyncMock, return_value=True) as debit_mock:
        result = await gateway._settle_billing(
            request, pending, response, execution_failed=False
        )

    debit_mock.assert_called_once()
    assert result is response


@pytest.mark.asyncio
async def test_response_indicates_failure_detects_iserror_true():
    """Body iterator with isError=true triggers skip path."""
    body = b'{"jsonrpc":"2.0","id":1,"result":{"isError":true,"content":[]}}'

    response = MagicMock()

    async def _iter():
        yield body

    response.body_iterator = _iter()
    failed = await MCPGatewayMiddleware._response_indicates_failure(response)
    assert failed is True


@pytest.mark.asyncio
async def test_response_indicates_failure_no_error_returns_false():
    body = b'{"jsonrpc":"2.0","id":1,"result":{"isError":false,"content":[{"type":"text","text":"ok"}]}}'
    response = MagicMock()

    async def _iter():
        yield body

    response.body_iterator = _iter()
    failed = await MCPGatewayMiddleware._response_indicates_failure(response)
    assert failed is False


@pytest.mark.asyncio
async def test_response_indicates_failure_handles_unparseable_body():
    """Unparseable bodies default to ``billing proceeds`` (returns False)."""
    response = MagicMock()

    async def _iter():
        yield b"not-json"

    response.body_iterator = _iter()
    failed = await MCPGatewayMiddleware._response_indicates_failure(response)
    assert failed is False
