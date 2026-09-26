# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from labeling import anchor
from labeling.commitments import merkle_root

_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
_ROOT = "ab" * 32
_TX = "0x" + "cd" * 32
_ADDRESS = "0x" + "ef" * 20


def _leaf(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _async_cm(value):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _mock_pool(monkeypatch):
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.transaction.return_value = _async_cm(None)
    pool = MagicMock()
    pool.acquire.return_value = _async_cm(conn)
    monkeypatch.setattr(anchor, "_get_pool", lambda: pool)
    return conn


def _source(name: str, pending: list[dict], leaves: list[dict] | None = None, assigned: int | None = None):
    source = MagicMock()
    source.name = name
    source.pending = AsyncMock(return_value=pending)
    source.assign = AsyncMock(side_effect=lambda _conn, _batch, items: len(items) if assigned is None else assigned)
    source.leaves = AsyncMock(return_value=leaves or [])
    return source


def test_table_source_rejects_unsafe_identifiers():
    with pytest.raises(ValueError):
        anchor.TableAnchorSource(name="bad", table="labeling_predictions; DROP", ordered_by="prediction_at")


def test_labeling_predictions_source_is_registered():
    assert "labeling_predictions" in anchor._SOURCES


@pytest.mark.anyio
async def test_seal_merges_sources_in_time_order(monkeypatch):
    conn = _mock_pool(monkeypatch)
    first = _source("a", [{"id": "a-2", "leaf_sha256": _leaf("a-2"), "ordered_at": _NOW + timedelta(seconds=2)}])
    second = _source(
        "b",
        [
            {"id": "b-1", "leaf_sha256": _leaf("b-1"), "ordered_at": _NOW + timedelta(seconds=1)},
            {"id": "b-3", "leaf_sha256": _leaf("b-3"), "ordered_at": _NOW + timedelta(seconds=3)},
        ],
    )
    monkeypatch.setattr(anchor, "_SOURCES", {"a": first, "b": second})

    batch_id = await anchor.seal_batch(8453)

    assert batch_id is not None
    insert = conn.execute.await_args_list[1]
    assert "INSERT INTO commitment_batches" in insert.args[0]
    assert insert.args[2:] == (merkle_root([_leaf("b-1"), _leaf("a-2"), _leaf("b-3")]), 3, 8453)
    assert conn.execute.await_args_list[0].args == ("SELECT pg_advisory_xact_lock($1)", anchor._SEAL_LOCK_KEY)
    assert first.assign.await_args.args[2] == [("a-2", 1)]
    assert second.assign.await_args.args[2] == [("b-1", 0), ("b-3", 2)]


@pytest.mark.anyio
async def test_seal_without_pending_leaves_creates_no_batch(monkeypatch):
    conn = _mock_pool(monkeypatch)
    monkeypatch.setattr(anchor, "_SOURCES", {"a": _source("a", [])})

    assert await anchor.seal_batch(8453) is None
    assert conn.execute.await_count == 1


@pytest.mark.anyio
async def test_seal_fails_closed_when_assignment_races(monkeypatch):
    _mock_pool(monkeypatch)
    source = _source("a", [{"id": "a-1", "leaf_sha256": _leaf("a-1"), "ordered_at": _NOW}], assigned=0)
    monkeypatch.setattr(anchor, "_SOURCES", {"a": source})

    with pytest.raises(RuntimeError):
        await anchor.seal_batch(8453)


@pytest.mark.anyio
async def test_batch_leaves_orders_across_sources_and_rejects_gaps(monkeypatch):
    _mock_pool(monkeypatch)
    first = _source("a", [], leaves=[{"anchor_leaf_index": 1, "leaf_sha256": "l1"}])
    second = _source("b", [], leaves=[{"anchor_leaf_index": 0, "leaf_sha256": "l0"}])
    monkeypatch.setattr(anchor, "_SOURCES", {"a": first, "b": second})

    assert await anchor.batch_leaves("batch-1", 2) == ["l0", "l1"]
    with pytest.raises(RuntimeError):
        await anchor.batch_leaves("batch-1", 3)


def _patch_tick(monkeypatch, *, claims, send):
    monkeypatch.setattr(anchor, "anchor_chain_id", lambda: 8453)
    monkeypatch.setattr(anchor, "seal_batch", AsyncMock(return_value=None))
    monkeypatch.setattr(anchor, "_claim_unsent_batch", AsyncMock(side_effect=claims))
    monkeypatch.setattr(anchor, "_send_root", send)
    record = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(anchor, "_record_submission", record)
    monkeypatch.setattr(anchor, "_release_unsent", release)
    monkeypatch.setattr(anchor, "_list_submitted", AsyncMock(return_value=[]))
    return record, release


@pytest.mark.anyio
async def test_tick_records_submission(monkeypatch):
    batch = {"id": "batch-1", "merkle_root": _ROOT, "chain_id": 8453}
    record, release = _patch_tick(monkeypatch, claims=[batch, None], send=AsyncMock(return_value=(_TX, _ADDRESS)))

    await anchor.anchor_tick()

    record.assert_awaited_once_with("batch-1", _TX, _ADDRESS)
    release.assert_not_awaited()


@pytest.mark.anyio
async def test_tick_releases_and_stops_on_send_failure_without_leaking_details(monkeypatch):
    batch = {"id": "batch-1", "merkle_root": _ROOT, "chain_id": 8453}
    claim = [batch, batch]
    record, release = _patch_tick(
        monkeypatch,
        claims=claim,
        send=AsyncMock(side_effect=RuntimeError("secret-api-key-value")),
    )

    await anchor.anchor_tick()

    record.assert_not_awaited()
    release.assert_awaited_once_with("batch-1", "anchor send failed: RuntimeError")
    assert anchor._claim_unsent_batch.await_count == 1


@pytest.mark.anyio
async def test_tick_rejects_unexpected_transaction_reference(monkeypatch):
    batch = {"id": "batch-1", "merkle_root": _ROOT, "chain_id": 8453}
    record, release = _patch_tick(monkeypatch, claims=[batch], send=AsyncMock(return_value=("0xBAD", _ADDRESS)))

    await anchor.anchor_tick()

    record.assert_not_awaited()
    release.assert_awaited_once_with("batch-1", "anchor send failed: ValueError")


def _submitted(lease_expired: bool = False) -> dict:
    return {
        "id": "batch-1",
        "merkle_root": _ROOT,
        "chain_id": 8453,
        "tx_hash": _TX,
        "anchor_address": _ADDRESS,
        "lease_expired": lease_expired,
    }


def _patch_rpc(monkeypatch, responses: dict):
    monkeypatch.setattr(anchor, "_rpc_url", lambda _chain: "https://rpc.test")
    monkeypatch.setattr(anchor, "_rpc", AsyncMock(side_effect=lambda _c, _u, method, _p: responses.get(method)))
    reset = AsyncMock()
    confirm = AsyncMock()
    monkeypatch.setattr(anchor, "_reset_submission", reset)
    monkeypatch.setattr(anchor, "_record_confirmation", confirm)
    return reset, confirm


_TX_BODY = {"blockNumber": "0x10", "input": f"0x{_ROOT}", "from": _ADDRESS, "to": _ADDRESS}


@pytest.mark.anyio
async def test_confirm_records_block_and_timestamp(monkeypatch):
    reset, confirm = _patch_rpc(
        monkeypatch,
        {
            "eth_getTransactionByHash": _TX_BODY,
            "eth_getTransactionReceipt": {"status": "0x1", "blockNumber": "0x10"},
            "eth_getBlockByNumber": {"timestamp": hex(int(_NOW.timestamp()))},
        },
    )

    assert await anchor._confirm(MagicMock(), _submitted()) is True
    confirm.assert_awaited_once_with("batch-1", _TX, 16, _NOW)
    reset.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tx_body", "receipt_status"),
    [
        (_TX_BODY, "0x0"),
        ({**_TX_BODY, "input": "0x" + "00" * 32}, "0x1"),
        ({**_TX_BODY, "from": "0x" + "11" * 20}, "0x1"),
    ],
)
async def test_confirm_resets_reverted_or_mismatched_transactions(monkeypatch, tx_body, receipt_status):
    reset, confirm = _patch_rpc(
        monkeypatch,
        {"eth_getTransactionByHash": tx_body, "eth_getTransactionReceipt": {"status": receipt_status, "blockNumber": "0x10"}},
    )

    assert await anchor._confirm(MagicMock(), _submitted()) is False
    reset.assert_awaited_once()
    confirm.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(("lease_expired", "resets"), [(True, 1), (False, 0)])
async def test_confirm_resets_dropped_transaction_only_after_lease(monkeypatch, lease_expired, resets):
    reset, confirm = _patch_rpc(monkeypatch, {})

    assert await anchor._confirm(MagicMock(), _submitted(lease_expired)) is False
    assert reset.await_count == resets
    confirm.assert_not_awaited()


@pytest.mark.anyio
async def test_confirm_waits_for_pending_transaction(monkeypatch):
    reset, confirm = _patch_rpc(monkeypatch, {"eth_getTransactionByHash": {**_TX_BODY, "blockNumber": None}})

    assert await anchor._confirm(MagicMock(), _submitted(True)) is False
    reset.assert_not_awaited()
    confirm.assert_not_awaited()
