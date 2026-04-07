# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Calculate tool – safe arithmetic evaluation via AST."""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from tools.registry import ToolDefinition


# ─── Schemas ──────────────────────────────────────────────────────────────────


class CalculateInput(BaseModel):
    expression: str = Field(
        ...,
        description="A safe arithmetic expression, e.g. '(3 + 4) * 2 / sqrt(9)'",
        max_length=200,
    )

    @field_validator("expression")
    @classmethod
    def _no_builtins_abuse(cls, v: str) -> str:
        allowed = re.compile(r"^[\d\s\.\+\-\*/\(\)\^%,a-z_]+$", re.IGNORECASE)
        if not allowed.match(v):
            raise ValueError("Expression contains disallowed characters.")
        return v


class CalculateOutput(BaseModel):
    expression: str
    result: float | None = None
    error: str | None = None


# ─── Safe evaluator ──────────────────────────────────────────────────────────

_SAFE_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_FUNCS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "pi": math.pi,
    "e": math.e,
}


def _safe_eval(node: ast.expr) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name) and node.id in _SAFE_FUNCS:
        val = _SAFE_FUNCS[node.id]
        if isinstance(val, float):
            return val
        raise ValueError(f"'{node.id}' is a function, not a constant.")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Unsupported call.")
        fn = _SAFE_FUNCS.get(node.func.id)
        if fn is None:
            raise ValueError(f"Function '{node.func.id}' is not allowed.")
        args = [_safe_eval(a) for a in node.args]
        return fn(*args)  # type: ignore[operator]
    if isinstance(node, ast.BinOp):
        op_fn = _SAFE_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Operator {type(node.op).__name__} not allowed.")
        return op_fn(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op_fn = _SAFE_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unary operator {type(node.op).__name__} not allowed.")
        return op_fn(_safe_eval(node.operand))
    raise ValueError(f"Unsupported node type: {type(node).__name__}")


# ─── Implementation ──────────────────────────────────────────────────────────


async def calculate(expression: str) -> dict[str, Any]:
    """Evaluate a safe arithmetic expression and return the numeric result."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _safe_eval(tree.body)
        return {"expression": expression, "result": result}
    except ZeroDivisionError:
        return {"expression": expression, "error": "Division by zero."}
    except Exception as exc:
        return {"expression": expression, "error": str(exc)}


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="calculate",
    version="1.0.0",
    description="Evaluate a safe arithmetic expression. Supports +,-,*,/,**,%,sqrt,abs,round,floor,ceil,log,sin,cos,tan,pi,e.",
    tags=["math", "arithmetic", "calculation"],
    input_schema=CalculateInput,
    output_schema=CalculateOutput,
    implementation=calculate,
)
