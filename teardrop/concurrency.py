# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Process-local admission control for agent runs."""

from __future__ import annotations

from contextlib import asynccontextmanager
from functools import wraps
from threading import Lock
from typing import AsyncIterator, Awaitable, Callable, ParamSpec, TypeVar


class AgentRunCapacityError(RuntimeError):
    """Raised when this process has no available agent-run capacity."""


class _AgentRunLease:
    def __init__(self, limiter: "_AgentRunLimiter") -> None:
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter.release()


class _AgentRunLimiter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._lock = Lock()

    def try_acquire(self) -> _AgentRunLease | None:
        with self._lock:
            if self._active >= self._limit:
                return None
            self._active += 1
        return _AgentRunLease(self)

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("Agent run limiter released without an active lease")
            self._active -= 1


_limiter = _AgentRunLimiter(16)


def init_agent_run_limiter(limit: int) -> None:
    """Initialize process-local agent run capacity during application startup."""
    if limit < 1:
        raise ValueError("Agent run concurrency limit must be positive")
    global _limiter
    _limiter = _AgentRunLimiter(limit)


def try_acquire_agent_run_slot() -> _AgentRunLease | None:
    """Return a lease immediately, or None when this process is saturated."""
    return _limiter.try_acquire()


@asynccontextmanager
async def acquire_agent_run_slot() -> AsyncIterator[None]:
    """Acquire a run slot without waiting and release it on every exit path."""
    lease = try_acquire_agent_run_slot()
    if lease is None:
        raise AgentRunCapacityError("Agent run capacity is exhausted")
    try:
        yield
    finally:
        lease.release()


_P = ParamSpec("_P")
_R = TypeVar("_R")


def limit_agent_runs(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
    """Apply shared non-waiting run admission to an async entry point."""

    @wraps(func)
    async def _wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        async with acquire_agent_run_slot():
            return await func(*args, **kwargs)

    return _wrapped
