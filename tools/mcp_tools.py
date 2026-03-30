"""Tool definitions using FastMCP and LangChain tool adapters.

Each tool is defined as a FastMCP server tool and separately as a
LangChain StructuredTool so it can be bound directly to the LLM in the graph.

Available tools
---------------
* calculate      – safe arithmetic evaluation
* get_datetime   – current UTC date and time
* web_search     – stub for external web search (returns placeholder)
* summarize_text – basic text statistics (word count, sentences, etc.)
"""

from __future__ import annotations

import ast
import logging
import math
import operator
import re
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ─── Input schemas ────────────────────────────────────────────────────────────

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


class GetDatetimeInput(BaseModel):
    format: str = Field(
        default="%Y-%m-%d %H:%M:%S UTC",
        description="strftime format string for the output",
        max_length=100,
    )


class WebSearchInput(BaseModel):
    query: str = Field(..., description="Search query", max_length=500)
    num_results: int = Field(default=5, ge=1, le=20)


class SummarizeTextInput(BaseModel):
    text: str = Field(..., description="Text to summarize", max_length=10_000)


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


# ─── Tool implementations ─────────────────────────────────────────────────────

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


async def get_datetime(format: str = "%Y-%m-%d %H:%M:%S UTC") -> dict[str, str]:
    """Return the current UTC date and time in the requested format."""
    now = datetime.now(tz=timezone.utc)
    try:
        formatted = now.strftime(format)
    except Exception:
        formatted = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    return {"datetime": formatted, "iso8601": now.isoformat()}


async def web_search(query: str, num_results: int = 5) -> dict[str, Any]:
    """Stub web search tool.  Returns a placeholder until a real search API is wired in."""
    logger.info("web_search called with query=%r (stub)", query)
    return {
        "query": query,
        "num_results": num_results,
        "results": [
            {
                "title": "Placeholder result – configure a real search API",
                "url": "https://example.com",
                "snippet": (
                    "This is a stub result. Set SERPER_API_KEY or TAVILY_API_KEY "
                    "in .env and replace this implementation."
                ),
            }
        ],
        "note": "web_search is a stub. Wire in a real provider to get live results.",
    }


async def summarize_text(text: str) -> dict[str, Any]:
    """Return basic statistics about the provided text."""
    words = text.split()
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return {
        "character_count": len(text),
        "word_count": len(words),
        "sentence_count": len(sentences),
        "paragraph_count": len(paragraphs),
        "average_words_per_sentence": round(len(words) / max(len(sentences), 1), 1),
    }


# ─── LangChain StructuredTool wrappers ───────────────────────────────────────

TOOLS: list[StructuredTool] = [
    StructuredTool.from_function(
        coroutine=calculate,
        name="calculate",
        description="Evaluate a safe arithmetic expression. Supports +,-,*,/,**,%,sqrt,abs,round,floor,ceil,log,sin,cos,tan,pi,e.",
        args_schema=CalculateInput,
    ),
    StructuredTool.from_function(
        coroutine=get_datetime,
        name="get_datetime",
        description="Return the current UTC date and time. Optional format parameter (strftime).",
        args_schema=GetDatetimeInput,
    ),
    StructuredTool.from_function(
        coroutine=web_search,
        name="web_search",
        description="Search the web for information. Returns titles, URLs, and snippets.",
        args_schema=WebSearchInput,
    ),
    StructuredTool.from_function(
        coroutine=summarize_text,
        name="summarize_text",
        description="Return word count, sentence count, and other statistics for a given text.",
        args_schema=SummarizeTextInput,
    ),
]


def get_langchain_tools() -> list[StructuredTool]:
    """Return the list of LangChain-compatible tool objects."""
    return TOOLS
