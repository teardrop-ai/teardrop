# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import teardrop.a2a_tasks as a2a_tasks


class _FakePool:
    def __init__(self, row):
        self.row = row
        self.rows = [row] if row is not None else []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        return self.row

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1"

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((query, args))
        return 0


class _ConflictPool(_FakePool):
    def __init__(self, row):
        super().__init__(row)
        self.fetchrow_results = [None, row]

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_results.pop(0)


def _row(**overrides):
    now = datetime.now(timezone.utc)
    value = {
        "id": "task-internal",
        "run_id": "run-1",
        "client_task_id": "client-task",
        "context_id": "ctx-1",
        "message": {"role": "user", "parts": []},
        "metadata": {},
        "user_message": "hello",
        "caller_org_id": "org-1",
        "caller_user_id": "user-1",
        "caller_ip": "198.51.100.7",
        "auth_method": "email",
        "task_state": "submitted",
        "output_text": "",
        "error": "",
        "usage_event_id": None,
        "cost_usdc": 0,
        "settlement_tx": "",
        "billing_method": "",
        "settlement_amount_usdc": 0,
        "duration_ms": 0,
        "worker_owner_id": "",
        "lease_expires_at": None,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "updated_at": now,
    }
    value.update(overrides)
    return value


@pytest.mark.anyio
async def test_create_inbound_task_keeps_client_id_separate(monkeypatch):
    pool = _FakePool(_row())
    monkeypatch.setattr(a2a_tasks, "_pool", pool)

    task, created = await a2a_tasks.create_inbound_task(
        task_id="task-internal",
        run_id="run-1",
        client_task_id="client-task",
        context_id="ctx-1",
        message={"role": "user", "parts": []},
        metadata={"source": "test"},
        user_message="hello",
        caller_org_id="org-1",
        caller_user_id="user-1",
        caller_ip="198.51.100.7",
        auth_method="email",
    )

    assert created
    assert task.id != task.client_task_id
    query, args = pool.fetchrow_calls[0]
    assert "INSERT INTO a2a_inbound_tasks" in query
    assert args[0:4] == ("task-internal", "run-1", "client-task", "ctx-1")


@pytest.mark.anyio
async def test_create_inbound_task_resolves_existing_client_id_conflict(monkeypatch):
    pool = _ConflictPool(_row())
    monkeypatch.setattr(a2a_tasks, "_pool", pool)

    task, created = await a2a_tasks.create_inbound_task(
        task_id="new-internal-task",
        run_id="run-2",
        client_task_id="client-task",
        context_id="ctx-1",
        message={"role": "user", "parts": []},
        metadata={},
        user_message="hello",
        caller_org_id="org-1",
        caller_user_id="user-1",
        caller_ip="198.51.100.7",
        auth_method="email",
    )

    assert not created
    assert task.id == "task-internal"
    assert "SELECT" in pool.fetchrow_calls[1][0]


@pytest.mark.anyio
async def test_authenticated_lookup_requires_org_and_user(monkeypatch):
    pool = _FakePool(_row())
    monkeypatch.setattr(a2a_tasks, "_pool", pool)

    assert await a2a_tasks.get_inbound_task("task-internal", caller_org_id="org-1") is None
    assert not pool.fetchrow_calls

    task = await a2a_tasks.get_inbound_task(
        "task-internal",
        caller_org_id="org-1",
        caller_user_id="user-1",
    )
    assert task is not None
    assert "caller_org_id = $2 AND caller_user_id = $3" in pool.fetchrow_calls[0][0]


@pytest.mark.anyio
async def test_claim_and_finish_limit_state_transitions(monkeypatch):
    pool = _FakePool(_row(task_state="running"))
    monkeypatch.setattr(a2a_tasks, "_pool", pool)

    claimed = await a2a_tasks.claim_inbound_task("task-internal")
    finished = await a2a_tasks.finish_inbound_task(
        "task-internal",
        task_state="completed",
        output_text="result",
        cost_usdc=-1,
        duration_ms=-2,
    )

    assert claimed is not None
    assert finished is not None
    assert "task_state = 'running'" in pool.fetchrow_calls[0][0]
    assert "task_state IN ('submitted', 'running')" in pool.fetchrow_calls[1][0]
    assert pool.fetchrow_calls[1][1][5:7] == (0, "")


@pytest.mark.anyio
async def test_recover_orphaned_tasks_marks_active_rows_failed(monkeypatch):
    pool = _FakePool(_row(task_state="running"))
    monkeypatch.setattr(a2a_tasks, "_pool", pool)

    recovered = await a2a_tasks.recover_orphaned_inbound_tasks()

    assert [task.id for task in recovered] == ["task-internal"]
    assert "task_state IN ('submitted', 'running')" in pool.fetch_calls[0][0]


@pytest.mark.anyio
async def test_mark_inbound_task_billing_method_persists_only_the_rail(monkeypatch):
    pool = _FakePool(_row(task_state="running"))
    monkeypatch.setattr(a2a_tasks, "_pool", pool)

    await a2a_tasks.mark_inbound_task_billing_method("task-internal", "x402")

    query, args = pool.execute_calls[0]
    assert "billing_method = $2" in query
    assert args == ("task-internal", "x402")


@pytest.mark.anyio
async def test_worker_pool_enforces_queue_capacity():
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def running_task():
        started.set()
        await release.wait()

    async def queued_task():
        completed.set()

    await a2a_tasks.start_inbound_task_workers(max_concurrency=1, queue_size=1)
    try:
        assert await a2a_tasks.enqueue_inbound_task("task-1", running_task)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await a2a_tasks.enqueue_inbound_task("task-2", queued_task)
        assert not await a2a_tasks.enqueue_inbound_task("task-3", queued_task)
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1)
    finally:
        await a2a_tasks.stop_inbound_task_workers()


@pytest.mark.anyio
async def test_stop_workers_finalizes_active_and_queued_tasks(monkeypatch):
    started = asyncio.Event()
    interrupted = []

    async def running_task():
        started.set()
        await asyncio.Event().wait()

    async def record_interruption(task_id):
        interrupted.append(task_id)

    monkeypatch.setattr(a2a_tasks, "_mark_worker_interrupted", record_interruption)
    await a2a_tasks.start_inbound_task_workers(max_concurrency=1, queue_size=1)
    try:
        assert await a2a_tasks.enqueue_inbound_task("active-task", running_task)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await a2a_tasks.enqueue_inbound_task("queued-task", running_task)
    finally:
        await a2a_tasks.stop_inbound_task_workers()

    assert sorted(interrupted) == ["active-task", "queued-task"]


def test_finish_rejects_non_terminal_state():
    with pytest.raises(ValueError, match="not terminal"):
        import asyncio

        asyncio.run(a2a_tasks.finish_inbound_task("task-internal", task_state="running"))
