# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Request-scoped dependencies for framework-agnostic agent nodes."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence


@dataclass(frozen=True, repr=False, slots=True)
class AgentRunContext:
    org_tools: Sequence[Any]
    org_tools_by_name: Mapping[str, Any]


_RUN_CONTEXT: ContextVar[AgentRunContext | None] = ContextVar("agent_run_context", default=None)


def get_agent_run_context() -> AgentRunContext:
    context = _RUN_CONTEXT.get()
    if context is None:
        raise RuntimeError("Agent run context is unavailable")
    return context


@contextmanager
def agent_run_context(context: AgentRunContext) -> Iterator[None]:
    token = _RUN_CONTEXT.set(context)
    try:
        yield
    finally:
        _RUN_CONTEXT.reset(token)
