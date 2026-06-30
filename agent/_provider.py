# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Internal planner-provider routing and invocation helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from langchain_core.messages import AIMessage

from agent.state import TaskStatus
from teardrop.llm_config import record_provider_failure as _record_provider_failure_impl

logger = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate-limited",
    "too many requests",
    "exceeded",
    "throttl",
    "quanta",
)


def _provider_api_key(settings, provider: str) -> str:
    """Resolve provider API key from global settings for per-turn overrides."""
    p = provider.lower()
    if p == "anthropic":
        return settings.anthropic_api_key or ""
    if p == "openai":
        return settings.openai_api_key or ""
    if p == "google":
        return settings.google_api_key or ""
    if p == "openrouter":
        return settings.openrouter_api_key or ""
    return ""


def _validate_schema_for_google(schema: Any, path: str = "$") -> list[str]:
    """Collect Gemini-incompatible array schema paths."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    if schema.get("type") == "array":
        items = schema.get("items")
        if not isinstance(items, dict) or not items:
            errors.append(f"{path}: array is missing a non-empty 'items' schema")
        elif "type" not in items and any(k in items for k in ("anyOf", "oneOf", "allOf")):
            errors.append(
                f"{path}: array 'items' must declare a concrete 'type' for Gemini compatibility "
                "(combinators like anyOf/oneOf/allOf are not supported)"
            )

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop_name, prop_schema in properties.items():
            errors.extend(_validate_schema_for_google(prop_schema, f"{path}.{prop_name}"))

    items = schema.get("items")
    if isinstance(items, dict):
        errors.extend(_validate_schema_for_google(items, f"{path}.items"))

    return errors


def _validate_tools_for_google(
    tools: list[Any],
    *,
    validate_schema_for_google: Callable[[Any, str], list[str]] = _validate_schema_for_google,
    logger_: logging.Logger = logger,
) -> None:
    """Fail fast if any bound tool has a Gemini-incompatible args schema."""
    violations: list[str] = []

    for idx, tool in enumerate(tools):
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is None:
            continue

        try:
            if hasattr(args_schema, "model_json_schema"):
                schema = args_schema.model_json_schema()
            elif hasattr(args_schema, "schema"):
                schema = args_schema.schema()
            else:
                continue
        except Exception as exc:
            logger_.warning("Skipping schema validation for tool %s: %s", getattr(tool, "name", idx), exc)
            continue

        errors = validate_schema_for_google(schema, "$")
        if errors:
            tool_name = getattr(tool, "name", f"index_{idx}")
            for err in errors:
                violations.append(f"{tool_name} (index {idx}) -> {err}")

    if violations:
        summary = "\n".join(violations)
        logger_.error("Gemini tool schema preflight failed:\n%s", summary)
        raise ValueError(
            f"Google/Gemini tool schema validation failed. Array parameters must include a non-empty 'items' schema.\n{summary}"
        )


def _bind_tools_for_provider(
    llm: Any,
    tools: list[Any],
    provider: str,
    *,
    validate_tools_for_google: Callable[[list[Any]], None] = _validate_tools_for_google,
) -> Any:
    """Bind tools with provider-specific preflight checks."""
    if provider == "google":
        validate_tools_for_google(tools)
    return llm.bind_tools(tools)


def _is_rate_limit_error(exc: Exception, *, markers: tuple[str, ...] = _RATE_LIMIT_MARKERS) -> bool:
    return any(marker in str(exc).lower() for marker in markers)


def _get_fallback_llm(
    *,
    failed_provider: str,
    failed_model: str,
    settings: Any,
    create_llm_from_config: Callable[[dict[str, Any]], Any],
    is_provider_cooled_down: Callable[[str, str], bool],
    provider_api_key: Callable[[Any, str], str] = _provider_api_key,
) -> tuple[Any, str, str] | None:
    """Return ``(llm, provider, model)`` for the first usable fallback in the pool, or ``None``."""
    for entry in settings.default_model_pool:
        provider = entry.get("provider", "")
        model = entry.get("model", "")
        if not provider or not model:
            continue
        if provider == failed_provider and model == failed_model:
            continue
        if is_provider_cooled_down(provider, model):
            continue

        api_key = provider_api_key(settings, provider)
        if not api_key:
            continue

        llm = create_llm_from_config(
            {
                "provider": provider,
                "model": model,
                "api_key": api_key,
                "max_tokens": settings.agent_max_tokens,
                "temperature": settings.agent_temperature,
                "timeout_seconds": settings.agent_llm_timeout_seconds,
            }
        )
        return llm, provider, model

    return None


async def _invoke_planner_llm(
    llm,
    messages: list,
    timeout_seconds: int,
    *,
    provider: str | None = None,
    model: str | None = None,
    is_rate_limit_error: Callable[[Exception], bool] = _is_rate_limit_error,
    record_provider_failure: Callable[[str, str], None] = _record_provider_failure_impl,
    logger_: logging.Logger = logger,
) -> AIMessage | dict[str, Any]:
    """Call the bound LLM with timeout + exception handling."""
    import time

    start_mono = time.monotonic()
    try:
        response = await asyncio.wait_for(  # type: ignore[return-value]
            llm.ainvoke(messages),
            timeout=timeout_seconds,
        )
        elapsed = int((time.monotonic() - start_mono) * 1000)
        logger_.info("planner_node: LLM call completed in %dms (provider=%s, model=%s)", elapsed, provider, model)
        return response
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start_mono) * 1000)
        logger_.error(
            "planner_node: LLM call timed out after %dms (timeout=%ss, provider=%s, model=%s)",
            elapsed,
            timeout_seconds,
            provider,
            model,
        )
        return {
            "messages": [AIMessage(content="The AI model timed out. Please try again.")],
            "task_status": TaskStatus.FAILED,
            "error": "LLM timeout",
            "error_type": "timeout",
        }
    except Exception as exc:
        elapsed = int((time.monotonic() - start_mono) * 1000)
        if provider and model and is_rate_limit_error(exc):
            record_provider_failure(provider, model)
            logger_.warning("planner_node: provider rate limited after %dms for %s/%s", elapsed, provider, model)
            return {
                "messages": [AIMessage(content=f"I encountered an error: {exc}")],
                "task_status": TaskStatus.FAILED,
                "error": str(exc),
                "error_type": "rate_limit",
            }
        logger_.error("planner_node error after %dms: %s", elapsed, exc)
        return {
            "messages": [AIMessage(content=f"I encountered an error: {exc}")],
            "task_status": TaskStatus.FAILED,
            "error": str(exc),
        }


def _resolve_planner_llm(
    state: Any,
    all_tools: list[Any],
    settings: Any,
    *,
    llm_config: dict | None,
    tool_iterations: int,
    synthesis_fast_reason: str | None,
    is_provider_cooled_down: Callable[[str, str], bool],
    get_fallback_llm: Callable[..., tuple[Any, str, str] | None],
    bind_tools_for_provider: Callable[[Any, list[Any], str], Any],
    get_llm_for_request: Callable[[dict | None], Any],
    create_llm_from_config: Callable[[dict[str, Any]], Any],
    provider_api_key: Callable[[Any, str], str] = _provider_api_key,
    logger_: logging.Logger = logger,
) -> tuple[Any, str, str, int, float, str | None]:
    """Resolve the planner LLM for one turn."""
    _ = state
    _cfg = llm_config or {}
    _provider = _cfg.get("provider", settings.agent_provider)
    _model = _cfg.get("model", settings.agent_model)

    if not llm_config and is_provider_cooled_down(_provider, _model):
        logger_.warning("Primary provider %s:%s is in cooldown; attempting fallback", _provider, _model)
        fallback_result = get_fallback_llm(failed_provider=_provider, failed_model=_model)
        if fallback_result is not None:
            fallback_llm, _provider, _model = fallback_result
            llm = bind_tools_for_provider(fallback_llm, all_tools, _provider)
        else:
            llm = bind_tools_for_provider(get_llm_for_request(llm_config), all_tools, _provider)
    else:
        llm = bind_tools_for_provider(get_llm_for_request(llm_config), all_tools, _provider)  # type: ignore[arg-type]

    _max_tokens = _cfg.get("max_tokens", settings.agent_max_tokens)
    _timeout = _cfg.get("timeout_seconds", settings.agent_llm_timeout_seconds)
    if tool_iterations > 0 and not llm_config:
        _max_tokens = settings.agent_synthesis_max_tokens
        _synth_provider = (settings.agent_synthesis_provider or "").strip()
        _synth_model = (settings.agent_synthesis_model or "").strip()
        if _synth_provider and _synth_model:
            _override_key = provider_api_key(settings, _synth_provider)
            if _override_key and not is_provider_cooled_down(_synth_provider, _synth_model):
                _provider, _model = _synth_provider, _synth_model
        llm_unbound = create_llm_from_config(
            {
                "provider": _provider,
                "model": _model,
                "api_key": provider_api_key(settings, _provider),
                "max_tokens": _max_tokens,
                "temperature": settings.agent_temperature,
                "timeout_seconds": _timeout,
            }
        )
        if synthesis_fast_reason:
            logger_.debug("planner_node: synthesis fast path active (reason=%s)", synthesis_fast_reason)
            llm = llm_unbound
        else:
            llm = bind_tools_for_provider(llm_unbound, all_tools, _provider)
    elif tool_iterations == 0 and not llm_config:
        _planner_provider = (settings.agent_planner_provider or "").strip()
        _planner_model = (settings.agent_planner_model or "").strip()
        if _planner_provider and _planner_model:
            _planner_key = provider_api_key(settings, _planner_provider)
            if _planner_key and not is_provider_cooled_down(_planner_provider, _planner_model):
                _provider, _model = _planner_provider, _planner_model
                llm = create_llm_from_config(
                    {
                        "provider": _provider,
                        "model": _model,
                        "api_key": _planner_key,
                        "max_tokens": _max_tokens,
                        "temperature": settings.agent_temperature,
                        "timeout_seconds": _timeout,
                    }
                )
                llm = bind_tools_for_provider(llm, all_tools, _provider)

    return llm, _provider, _model, _max_tokens, _timeout, synthesis_fast_reason
