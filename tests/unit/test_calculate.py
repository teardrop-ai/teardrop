"""Unit tests for tools/definitions/calculate.py — safe arithmetic evaluator."""

from __future__ import annotations

import math

import pytest

from tools.definitions.calculate import calculate


@pytest.mark.anyio
async def test_addition():
    result = await calculate("1 + 2")
    assert result["result"] == 3.0


@pytest.mark.anyio
async def test_subtraction():
    result = await calculate("10 - 4")
    assert result["result"] == 6.0


@pytest.mark.anyio
async def test_multiplication():
    result = await calculate("3 * 7")
    assert result["result"] == 21.0


@pytest.mark.anyio
async def test_division():
    result = await calculate("10 / 4")
    assert result["result"] == pytest.approx(2.5)


@pytest.mark.anyio
async def test_complex_expression():
    result = await calculate("(3 + 4) * 2 / sqrt(49)")
    assert result["result"] == pytest.approx(2.0)


@pytest.mark.anyio
async def test_sqrt():
    result = await calculate("sqrt(144)")
    assert result["result"] == pytest.approx(12.0)


@pytest.mark.anyio
async def test_pi_constant():
    result = await calculate("pi")
    assert result["result"] == pytest.approx(math.pi)


@pytest.mark.anyio
async def test_e_constant():
    result = await calculate("e")
    assert result["result"] == pytest.approx(math.e)


@pytest.mark.anyio
async def test_power():
    result = await calculate("2 ** 10")
    assert result["result"] == 1024.0


@pytest.mark.anyio
async def test_modulo():
    result = await calculate("10 % 3")
    assert result["result"] == 1.0


@pytest.mark.anyio
async def test_unary_negation():
    result = await calculate("-5 + 10")
    assert result["result"] == 5.0


@pytest.mark.anyio
async def test_floor_ceil():
    floor = await calculate("floor(3.7)")
    ceil_ = await calculate("ceil(3.2)")
    assert floor["result"] == 3.0
    assert ceil_["result"] == 4.0


@pytest.mark.anyio
async def test_division_by_zero_returns_error():
    result = await calculate("1 / 0")
    assert result.get("error") is not None
    assert "result" not in result or result.get("result") is None


@pytest.mark.anyio
async def test_injection_prevention_semicolon():
    from pydantic import ValidationError

    from tools.definitions.calculate import CalculateInput

    with pytest.raises(ValidationError):
        CalculateInput(expression="1 + 1; import os")


@pytest.mark.anyio
async def test_injection_prevention_underscore():
    from pydantic import ValidationError

    from tools.definitions.calculate import CalculateInput

    with pytest.raises(ValidationError):
        CalculateInput(expression="__import__('os')")


@pytest.mark.anyio
async def test_undefined_function_returns_error():
    result = await calculate("evil(1)")
    assert result.get("error") is not None
