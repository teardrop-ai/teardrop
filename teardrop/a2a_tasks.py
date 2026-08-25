# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Persistence helpers for asynchronous inbound A2A tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from shared.db_pool import PgPool

logger = logging.getLogger(__name__)

InboundTaskState = Literal[
    "submitted",
    "running",
    "completed",
    "failed",
    "timeout",
    "rejected_payment",
    "rejected_auth_credit",
]

TERMINAL_INBOUND_TASK_STATES = frozenset(
    {
        "completed",
        "failed",
        "timeout",
        "rejected_payment",
        "rejected_auth_credit",
    }
)

_pool: PgPool | None = None
_task_queue: asyncio.Queue[tuple[str, Callable[[], Awaitable[None]]]] | None = None
_worker_tasks: set[asyncio.Task[None]] = set()
_worker_heartbeat_task: asyncio.Task[None] | None = None
_worker_owner_id = f"a2a-{uuid.uuid4()}"
_task_lease_seconds = 60

_TASK_COLUMNS = (
    "id, run_id, client_task_id, context_id, message, metadata, user_message, "
    "caller_org_id, caller_user_id, caller_ip, auth_method, task_state, output_text, "
    "error, usage_event_id, cost_usdc, settlement_tx, billing_method, settlement_amount_usdc, "
    "duration_ms, worker_owner_id, lease_expires_at, created_at, started_at, finished_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class A2AInboundTask:
    id: str
    run_id: str
    client_task_id: str
    context_id: str
    message: dict[str, Any]
    metadata: dict[str, Any]
    user_message: str
    caller_org_id: str
    caller_user_id: str
    caller_ip: str
    auth_method: str
    task_state: InboundTaskState
    output_text: str
    error: str
    usage_event_id: str | None
    cost_usdc: int
    settlement_tx: str
    billing_method: str
    settlement_amount_usdc: int
    duration_ms: int
    worker_owner_id: str
    lease_expires_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


def init_a2a_tasks_db(pool: PgPool, *, lease_seconds: int = 60) -> None:
    """Bind the application pool after migrations have completed."""
    if lease_seconds <= 0:
        raise ValueError("A2A task lease duration must be positive")
    global _pool, _task_lease_seconds
    _pool = pool
    _task_lease_seconds = lease_seconds


def close_a2a_tasks_db() -> None:
    """Release the shared pool reference before application shutdown."""
    global _pool
    _pool = None


def _get_pool() -> PgPool:
    if _pool is None:
        raise RuntimeError("A2A task DB not initialised - call init_a2a_tasks_db() first")
    return _pool


def _row_to_task(row: Any) -> A2AInboundTask:
    data = dict(row)
    message = data["message"]
    metadata = data["metadata"]
    data["message"] = json.loads(message) if isinstance(message, str) else dict(message)
    data["metadata"] = json.loads(metadata) if isinstance(metadata, str) else dict(metadata)
    return A2AInboundTask(**data)


async def create_inbound_task(
    *,
    task_id: str,
    run_id: str,
    client_task_id: str | None,
    context_id: str | None,
    message: dict[str, Any],
    metadata: dict[str, Any],
    user_message: str,
    caller_org_id: str,
    caller_user_id: str,
    caller_ip: str,
    auth_method: str,
) -> tuple[A2AInboundTask, bool]:
    pool = _get_pool()
    row = await pool.fetchrow(
        f"""
        INSERT INTO a2a_inbound_tasks (
            id, run_id, client_task_id, context_id, message, metadata, user_message,
            caller_org_id, caller_user_id, caller_ip, auth_method, worker_owner_id,
            lease_expires_at
        )
        VALUES ($1, $2, $3, $4, $5::JSONB, $6::JSONB, $7, $8, $9, $10, $11, $12,
                NOW() + make_interval(secs => $13))
        ON CONFLICT DO NOTHING
        RETURNING {_TASK_COLUMNS}
        """,
        task_id,
        run_id,
        client_task_id or "",
        context_id or "",
        json.dumps(message),
        json.dumps(metadata),
        user_message,
        caller_org_id,
        caller_user_id,
        caller_ip,
        auth_method or "anonymous",
        _worker_owner_id,
        _task_lease_seconds,
    )
    if row is not None:
        return _row_to_task(row), True

    if not client_task_id:
        raise RuntimeError("Inbound A2A task insert was not applied")
    if caller_org_id and caller_user_id:
        row = await pool.fetchrow(
            f"""
            SELECT {_TASK_COLUMNS}
            FROM a2a_inbound_tasks
            WHERE caller_org_id = $1 AND caller_user_id = $2 AND client_task_id = $3
            ORDER BY created_at, id
            LIMIT 1
            """,
            caller_org_id,
            caller_user_id,
            client_task_id,
        )
    else:
        row = await pool.fetchrow(
            f"""
            SELECT {_TASK_COLUMNS}
            FROM a2a_inbound_tasks
            WHERE caller_org_id = '' AND caller_user_id = ''
              AND caller_ip = $1 AND client_task_id = $2
            ORDER BY created_at, id
            LIMIT 1
            """,
            caller_ip,
            client_task_id,
        )
    if row is None:
        raise RuntimeError("Inbound A2A task insert conflict could not be resolved")
    return _row_to_task(row), False


async def get_inbound_task(
    task_id: str,
    *,
    caller_org_id: str | None = None,
    caller_user_id: str | None = None,
    anonymous_only: bool = False,
) -> A2AInboundTask | None:
    """Fetch a task, enforcing authenticated caller ownership when supplied.

    Anonymous callers use the unguessable internal task ID as a capability. An
    authenticated lookup must provide both JWT-derived organization and user
    identifiers so a task cannot be read across either boundary.
    """
    pool = _get_pool()
    if anonymous_only:
        row = await pool.fetchrow(
            f"""
            SELECT {_TASK_COLUMNS}
            FROM a2a_inbound_tasks
            WHERE id = $1 AND caller_org_id = '' AND caller_user_id = ''
            """,
            task_id,
        )
    elif caller_org_id is None and caller_user_id is None:
        row = await pool.fetchrow(
            f"SELECT {_TASK_COLUMNS} FROM a2a_inbound_tasks WHERE id = $1",
            task_id,
        )
    elif caller_org_id and caller_user_id:
        row = await pool.fetchrow(
            f"""
            SELECT {_TASK_COLUMNS}
            FROM a2a_inbound_tasks
            WHERE id = $1 AND caller_org_id = $2 AND caller_user_id = $3
            """,
            task_id,
            caller_org_id,
            caller_user_id,
        )
    else:
        return None
    return _row_to_task(row) if row is not None else None


async def claim_inbound_task(task_id: str) -> A2AInboundTask | None:
    """Atomically move one submitted task to running."""
    pool = _get_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE a2a_inbound_tasks
        SET task_state = 'running', started_at = COALESCE(started_at, NOW()),
            lease_expires_at = NOW() + make_interval(secs => $3), updated_at = NOW()
        WHERE id = $1 AND task_state = 'submitted'
          AND worker_owner_id = $2 AND lease_expires_at > NOW()
        RETURNING {_TASK_COLUMNS}
        """,
        task_id,
        _worker_owner_id,
        _task_lease_seconds,
    )
    return _row_to_task(row) if row is not None else None


async def renew_inbound_task_leases() -> int:
    """Extend leases for active tasks owned by this application process."""
    pool = _get_pool()
    result = await pool.execute(
        """
        UPDATE a2a_inbound_tasks
        SET lease_expires_at = NOW() + make_interval(secs => $2), updated_at = NOW()
        WHERE worker_owner_id = $1 AND task_state IN ('submitted', 'running')
        """,
        _worker_owner_id,
        _task_lease_seconds,
    )
    return int(str(result).rsplit(" ", 1)[-1])


async def mark_inbound_task_billing_method(task_id: str, billing_method: str) -> None:
    """Record the selected billing rail without persisting payment credentials."""
    if billing_method not in {"credit", "x402"}:
        raise ValueError(f"Unsupported billing method: {billing_method}")
    pool = _get_pool()
    await pool.execute(
        """
        UPDATE a2a_inbound_tasks
        SET billing_method = $2, updated_at = NOW()
        WHERE id = $1 AND task_state = 'running'
        """,
        task_id,
        billing_method,
    )


async def finish_inbound_task(
    task_id: str,
    *,
    task_state: InboundTaskState,
    output_text: str = "",
    error: str = "",
    usage_event_id: str | None = None,
    cost_usdc: int = 0,
    settlement_tx: str = "",
    billing_method: str = "",
    settlement_amount_usdc: int = 0,
    duration_ms: int = 0,
) -> A2AInboundTask | None:
    """Persist a terminal task outcome and return the updated row."""
    if task_state not in TERMINAL_INBOUND_TASK_STATES:
        raise ValueError(f"Task state is not terminal: {task_state}")
    pool = _get_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE a2a_inbound_tasks
        SET task_state = $2,
            output_text = $3,
            error = $4,
            usage_event_id = $5,
            cost_usdc = $6,
            settlement_tx = $7,
            billing_method = $8,
            settlement_amount_usdc = $9,
            duration_ms = $10,
            finished_at = COALESCE(finished_at, NOW()),
            lease_expires_at = NOW(),
            updated_at = NOW()
        WHERE id = $1 AND task_state IN ('submitted', 'running')
        RETURNING {_TASK_COLUMNS}
        """,
        task_id,
        task_state,
        output_text[:65536],
        error[:1024],
        usage_event_id,
        max(0, cost_usdc),
        settlement_tx,
        billing_method,
        max(0, settlement_amount_usdc),
        max(0, duration_ms),
    )
    return _row_to_task(row) if row is not None else None


async def recover_orphaned_inbound_tasks() -> list[A2AInboundTask]:
    """Fail active tasks whose process lease has expired."""
    pool = _get_pool()
    rows = await pool.fetch(
        f"""
        UPDATE a2a_inbound_tasks
        SET task_state = 'failed',
            output_text = 'Task interrupted by process restart.',
            error = 'Task interrupted by process restart; billing outcome is unknown.',
            finished_at = COALESCE(finished_at, NOW()),
                        lease_expires_at = NOW(),
            updated_at = NOW()
                WHERE task_state IN ('submitted', 'running')
                    AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
        RETURNING {_TASK_COLUMNS}
        """
    )
    return [_row_to_task(row) for row in rows]


async def delete_terminal_inbound_tasks(*, ttl_days: int, batch_size: int, pool: PgPool | None = None) -> int:
    """Delete a bounded batch of old terminal task projections."""
    if ttl_days <= 0 or batch_size <= 0:
        return 0
    task_pool = pool or _get_pool()
    return int(
        await task_pool.fetchval(
            """
            WITH candidates AS (
                SELECT ctid
                FROM a2a_inbound_tasks
                WHERE task_state IN ('completed', 'failed', 'timeout', 'rejected_payment', 'rejected_auth_credit')
                  AND created_at < NOW() - make_interval(days => $1)
                ORDER BY created_at, id
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            ), deleted AS (
                DELETE FROM a2a_inbound_tasks
                WHERE ctid IN (SELECT ctid FROM candidates)
                RETURNING 1
            )
            SELECT COUNT(*) FROM deleted
            """,
            ttl_days,
            batch_size,
        )
        or 0
    )


async def cleanup_terminal_inbound_tasks(*, ttl_days: int, batch_size: int, pool: PgPool | None = None) -> int:
    """Delete all eligible terminal task rows in bounded batches."""
    total_deleted = 0
    while True:
        deleted = await delete_terminal_inbound_tasks(ttl_days=ttl_days, batch_size=batch_size, pool=pool)
        total_deleted += deleted
        if deleted < batch_size:
            return total_deleted


async def _mark_worker_interrupted(task_id: str) -> None:
    try:
        await finish_inbound_task(
            task_id,
            task_state="failed",
            error="Task worker stopped before completion",
        )
    except Exception:
        logger.warning("Failed to finalize interrupted inbound A2A task task_id=%s", task_id, exc_info=True)


async def _inbound_task_worker(queue: asyncio.Queue[tuple[str, Callable[[], Awaitable[None]]]]) -> None:
    while True:
        item: tuple[str, Callable[[], Awaitable[None]]] | None = None
        try:
            item = await queue.get()
            await item[1]()
        except asyncio.CancelledError:
            if item is not None:
                await _mark_worker_interrupted(item[0])
            raise
        except Exception:
            if item is not None:
                await _mark_worker_interrupted(item[0])
                logger.exception("Inbound A2A worker failed task_id=%s", item[0])
        finally:
            if item is not None:
                queue.task_done()


async def _inbound_task_heartbeat() -> None:
    while True:
        try:
            await asyncio.sleep(max(1.0, _task_lease_seconds / 3))
        except asyncio.CancelledError:
            raise
        try:
            renewed = await renew_inbound_task_leases()
            if renewed:
                logger.debug("Renewed %d inbound A2A task lease(s)", renewed)
        except Exception:
            logger.warning("Inbound A2A task lease renewal failed", exc_info=True)


async def start_inbound_task_workers(*, max_concurrency: int, queue_size: int) -> None:
    """Start a bounded local worker pool for queued inbound A2A tasks."""
    global _task_queue, _worker_heartbeat_task
    if _worker_tasks:
        return
    if max_concurrency <= 0 or queue_size <= 0:
        raise ValueError("Inbound A2A worker limits must be positive")
    _task_queue = asyncio.Queue(maxsize=queue_size)
    for index in range(max_concurrency):
        task = asyncio.create_task(_inbound_task_worker(_task_queue), name=f"a2a-inbound-worker-{index}")
        _worker_tasks.add(task)
    _worker_heartbeat_task = asyncio.create_task(_inbound_task_heartbeat(), name="a2a-inbound-task-heartbeat")


async def enqueue_inbound_task(task_id: str, runner: Callable[[], Awaitable[None]]) -> bool:
    """Queue one persisted task without exceeding the local backlog limit."""
    if _task_queue is None:
        return False
    try:
        _task_queue.put_nowait((task_id, runner))
    except asyncio.QueueFull:
        return False
    return True


async def stop_inbound_task_workers() -> None:
    """Stop local workers and mark work that cannot continue as failed."""
    global _task_queue, _worker_heartbeat_task
    queue = _task_queue
    _task_queue = None

    heartbeat_task = _worker_heartbeat_task
    _worker_heartbeat_task = None
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

    if not _worker_tasks:
        return

    worker_tasks = tuple(_worker_tasks)
    for task in worker_tasks:
        task.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)
    _worker_tasks.clear()

    if queue is None:
        return
    while True:
        try:
            task_id, _runner = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            await _mark_worker_interrupted(task_id)
        finally:
            queue.task_done()
