# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared tool execution helpers and canonical result envelope."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
from jsonschema import Draft7Validator
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolResult:
    """Canonical execution result for all tool surfaces."""

    success: bool
    tool_name: str
    tool_call_id: str
    content: str
    elapsed_ms: int
    error_class: str | None = None
    retry_safe: bool = False
    billable: bool = True


def _resolve_timeout_seconds(tool: Any, explicit_timeout: float | None) -> float | None:
    if explicit_timeout is not None:
        return explicit_timeout
    meta = getattr(tool, "metadata", None)
    if isinstance(meta, dict):
        value = meta.get("timeout_seconds")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _resolve_output_schema(tool: Any, explicit_schema: Any = None) -> Any:
    if explicit_schema is not None:
        return explicit_schema
    meta = getattr(tool, "metadata", None)
    if isinstance(meta, dict):
        return meta.get("output_schema")
    return None


def _error_content(error_class: str, message: str, retry_safe: bool, billable: bool) -> str:
    return json.dumps(
        {
            "status": "error",
            "error_class": error_class,
            "message": message,
            "retry_safe": retry_safe,
            "billable": billable,
        }
    )


def _serialize_content(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except TypeError:
        return json.dumps(result, default=str)


def _classify_embedded_error(result: dict[str, Any]) -> tuple[str, bool, bool, str]:
    raw = str(result.get("error", "tool execution failed")).lower()
    if "timeout" in raw:
        return ("timeout", True, False, "Tool timed out")
    if "temporarily unavailable" in raw or "circuit breaker" in raw:
        return ("upstream_unavailable", True, False, "Tool is temporarily unavailable")
    if "url blocked" in raw or "decrypt" in raw or "request failed" in raw or "connection" in raw:
        return ("upstream_unavailable", True, False, "Tool upstream is unavailable")
    if "non-json" in raw or "invalid json" in raw:
        return ("upstream_unavailable", True, False, "Tool returned an invalid upstream response")
    return ("business_error", False, True, str(result.get("error", "Tool execution failed")))


def _validate_output_schema(result: Any, schema: Any) -> tuple[bool, str | None]:
    if schema is None:
        return True, None
    try:
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            schema.model_validate(result)
            return True, None
        if isinstance(schema, dict):
            Draft7Validator(schema).validate(result)
            return True, None
        return False, "Unsupported output schema type"
    except Exception:
        return False, "Tool output failed contract validation"


async def execute_tool(
    *,
    tool_name: str,
    tool_call_id: str,
    tool_args: dict[str, Any],
    tool: Any | None = None,
    invoke: Callable[..., Awaitable[Any]] | None = None,
    timeout_seconds: float | None = None,
    output_schema: Any = None,
) -> ToolResult:
    """Execute a tool with normalized timeout, error handling, and output validation."""
    start_mono = time.monotonic()

    if tool is None and invoke is None:
        return ToolResult(
            success=False,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=_error_content("blocked", f"Tool '{tool_name}' not found.", False, False),
            elapsed_ms=0,
            error_class="blocked",
            retry_safe=False,
            billable=False,
        )

    effective_timeout = _resolve_timeout_seconds(tool, timeout_seconds)

    async def _invoke() -> Any:
        if invoke is not None:
            return await invoke(**tool_args)
        return await tool.ainvoke(tool_args)

    try:
        if effective_timeout is not None:
            result = await asyncio.wait_for(_invoke(), timeout=effective_timeout)
        else:
            result = await _invoke()

        if isinstance(result, dict) and "error" in result:
            error_class, retry_safe, billable, message = _classify_embedded_error(result)
            return ToolResult(
                success=False,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                content=_error_content(error_class, message, retry_safe, billable),
                elapsed_ms=int((time.monotonic() - start_mono) * 1000),
                error_class=error_class,
                retry_safe=retry_safe,
                billable=billable,
            )

        resolved_output_schema = _resolve_output_schema(tool, output_schema)
        valid_output, output_error = _validate_output_schema(result, resolved_output_schema)
        if not valid_output:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                content=_error_content("output_contract_error", output_error or "Output contract failed", False, False),
                elapsed_ms=int((time.monotonic() - start_mono) * 1000),
                error_class="output_contract_error",
                retry_safe=False,
                billable=False,
            )

        return ToolResult(
            success=True,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=_serialize_content(result),
            elapsed_ms=int((time.monotonic() - start_mono) * 1000),
            error_class=None,
            retry_safe=False,
            billable=True,
        )
    except asyncio.TimeoutError:
        return ToolResult(
            success=False,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=_error_content("timeout", "Tool timed out", True, False),
            elapsed_ms=int((time.monotonic() - start_mono) * 1000),
            error_class="timeout",
            retry_safe=True,
            billable=False,
        )
    except PydanticValidationError:
        return ToolResult(
            success=False,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=_error_content("validation_error", f"Invalid arguments for tool '{tool_name}'", False, False),
            elapsed_ms=int((time.monotonic() - start_mono) * 1000),
            error_class="validation_error",
            retry_safe=False,
            billable=False,
        )
    except aiohttp.ClientError:
        logger.warning("tool %s upstream unavailable", tool_name, exc_info=True)
        return ToolResult(
            success=False,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=_error_content("upstream_unavailable", "Tool upstream is unavailable", True, False),
            elapsed_ms=int((time.monotonic() - start_mono) * 1000),
            error_class="upstream_unavailable",
            retry_safe=True,
            billable=False,
        )
    except Exception as exc:
        logger.warning("tool %s failed: %s", tool_name, exc)
        return ToolResult(
            success=False,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=_error_content("business_error", "Tool execution failed", False, True),
            elapsed_ms=int((time.monotonic() - start_mono) * 1000),
            error_class="business_error",
            retry_safe=False,
            billable=True,
        )
