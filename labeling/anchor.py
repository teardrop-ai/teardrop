# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Source-agnostic Merkle batching and Base anchoring for committed leaves.

Each tick seals unanchored leaves from every registered source into RFC 6962 batches,
sends each root as a 0-value self-transaction through CDP, and confirms it on-chain.
Re-sending the same root is harmless, so a lost or dropped send is simply retried.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from labeling.commitments import MAX_BATCH_LEAVES, merkle_root
from labeling.store import _get_pool
from shared.db_pool import Row
from teardrop.config import get_settings

logger = logging.getLogger(__name__)

_SEAL_LOCK_KEY = 0x564F5231
_SEND_LEASE_SECONDS = 900
_MAX_BATCHES_PER_TICK = 8
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_TX_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


class AnchorSource(Protocol):
    name: str

    async def pending(self, conn: Any, limit: int) -> list[Row]:
        """Lock and return unanchored rows as ``id``, ``leaf_sha256``, ``ordered_at``."""
        ...

    async def assign(self, conn: Any, batch_id: str, assignments: list[tuple[str, int]]) -> int: ...

    async def leaves(self, conn: Any, batch_id: str) -> list[Row]:
        """Return ``anchor_leaf_index`` and ``leaf_sha256`` rows for a batch."""
        ...


@dataclass(frozen=True, slots=True)
class TableAnchorSource:
    """Anchor source for a table implementing the contract in ``labeling.commitments``."""

    name: str
    table: str
    ordered_by: str

    def __post_init__(self) -> None:
        if not (_IDENTIFIER.fullmatch(self.table) and _IDENTIFIER.fullmatch(self.ordered_by)):
            raise ValueError("Anchor source identifiers must be simple SQL identifiers")

    async def pending(self, conn: Any, limit: int) -> list[Row]:
        return list(
            await conn.fetch(
                f"""
                SELECT id, leaf_sha256, {self.ordered_by} AS ordered_at
                FROM {self.table}
                WHERE leaf_sha256 IS NOT NULL AND anchor_batch_id IS NULL
                ORDER BY {self.ordered_by}, id
                LIMIT $1
                FOR UPDATE
                """,
                limit,
            )
        )

    async def assign(self, conn: Any, batch_id: str, assignments: list[tuple[str, int]]) -> int:
        result = await conn.execute(
            f"""
            UPDATE {self.table} AS t
            SET anchor_batch_id = $1, anchor_leaf_index = v.leaf_index
            FROM unnest($2::text[], $3::int[]) AS v(row_id, leaf_index)
            WHERE t.id = v.row_id AND t.anchor_batch_id IS NULL
            """,
            batch_id,
            [row_id for row_id, _ in assignments],
            [index for _, index in assignments],
        )
        return int(str(result).split()[-1])

    async def leaves(self, conn: Any, batch_id: str) -> list[Row]:
        return list(
            await conn.fetch(
                f"SELECT anchor_leaf_index, leaf_sha256 FROM {self.table} WHERE anchor_batch_id = $1",
                batch_id,
            )
        )


_SOURCES: dict[str, AnchorSource] = {}


def register_anchor_source(source: AnchorSource) -> None:
    _SOURCES[source.name] = source


register_anchor_source(TableAnchorSource(name="labeling_predictions", table="labeling_predictions", ordered_by="prediction_at"))


def anchor_chain_id() -> int:
    return 84532 if get_settings().cdp_network == "base-sepolia" else 8453


async def seal_batch(chain_id: int) -> str | None:
    """Assign up to MAX_BATCH_LEAVES unanchored leaves across all sources to a new batch."""
    async with _get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _SEAL_LOCK_KEY)
            pending: list[tuple[datetime, str, str, str]] = []
            for source in _SOURCES.values():
                for row in await source.pending(conn, MAX_BATCH_LEAVES):
                    pending.append((row["ordered_at"], source.name, str(row["id"]), str(row["leaf_sha256"])))
            if not pending:
                return None
            pending.sort(key=lambda item: (item[0], item[1], item[2]))
            selected = pending[:MAX_BATCH_LEAVES]
            batch_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO commitment_batches (id, merkle_root, leaf_count, chain_id) VALUES ($1, $2, $3, $4)",
                batch_id,
                merkle_root([item[3] for item in selected]),
                len(selected),
                chain_id,
            )
            by_source: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for index, (_, name, row_id, _) in enumerate(selected):
                by_source[name].append((row_id, index))
            for name, assignments in by_source.items():
                if await _SOURCES[name].assign(conn, batch_id, assignments) != len(assignments):
                    raise RuntimeError("Anchor leaf assignment did not cover every selected leaf")
    return batch_id


async def batch_leaves(batch_id: str, leaf_count: int) -> list[str]:
    """Return the ordered leaves of a batch across every registered source."""
    indexed: list[tuple[int, str]] = []
    async with _get_pool().acquire() as conn:
        for source in _SOURCES.values():
            indexed.extend(
                (int(row["anchor_leaf_index"]), str(row["leaf_sha256"])) for row in await source.leaves(conn, batch_id)
            )
    indexed.sort()
    if [index for index, _ in indexed] != list(range(leaf_count)):
        raise RuntimeError("Commitment batch leaves are incomplete")
    return [leaf for _, leaf in indexed]


async def _claim_unsent_batch() -> Row | None:
    return await _get_pool().fetchrow(
        """
        UPDATE commitment_batches
        SET lease_expires_at = NOW() + ($1 * INTERVAL '1 second'),
            attempts = attempts + 1
        WHERE id = (
            SELECT id FROM commitment_batches
            WHERE tx_hash IS NULL AND block_number IS NULL
              AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
            ORDER BY created_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, merkle_root, chain_id
        """,
        _SEND_LEASE_SECONDS,
    )


async def _record_submission(batch_id: str, tx_hash: str, anchor_address: str) -> None:
    await _get_pool().execute(
        """
        UPDATE commitment_batches
        SET tx_hash = $2, anchor_address = $3, last_error = ''
        WHERE id = $1 AND tx_hash IS NULL
        """,
        batch_id,
        tx_hash,
        anchor_address,
    )


async def _release_unsent(batch_id: str, error: str) -> None:
    await _get_pool().execute(
        "UPDATE commitment_batches SET lease_expires_at = NULL, last_error = $2 WHERE id = $1 AND tx_hash IS NULL",
        batch_id,
        error,
    )


async def _reset_submission(batch_id: str, tx_hash: str, error: str) -> None:
    await _get_pool().execute(
        """
        UPDATE commitment_batches
        SET tx_hash = NULL, anchor_address = NULL, lease_expires_at = NULL, last_error = $3
        WHERE id = $1 AND tx_hash = $2 AND block_number IS NULL
        """,
        batch_id,
        tx_hash,
        error,
    )


async def _record_confirmation(batch_id: str, tx_hash: str, block_number: int, anchored_at: datetime) -> None:
    await _get_pool().execute(
        """
        UPDATE commitment_batches
        SET block_number = $3, anchored_at = $4, lease_expires_at = NULL, last_error = ''
        WHERE id = $1 AND tx_hash = $2 AND block_number IS NULL
        """,
        batch_id,
        tx_hash,
        block_number,
        anchored_at,
    )


async def _list_submitted(limit: int) -> list[Row]:
    return list(
        await _get_pool().fetch(
            """
            SELECT id, merkle_root, chain_id, tx_hash, anchor_address,
                   COALESCE(lease_expires_at <= NOW(), TRUE) AS lease_expired
            FROM commitment_batches
            WHERE tx_hash IS NOT NULL AND block_number IS NULL
            ORDER BY created_at, id
            LIMIT $1
            """,
            limit,
        )
    )


async def _send_root(root: str, chain_id: int) -> tuple[str, str]:
    from cdp import CdpClient
    from cdp.evm_transaction_types import TransactionRequestEIP1559

    from teardrop.agent_wallets import _chain_id_to_network

    settings = get_settings()
    async with CdpClient() as cdp:
        account = await cdp.evm.get_or_create_account(name=settings.vor_anchor_cdp_account)
        address = str(account.address)
        tx_hash = await cdp.evm.send_transaction(
            address=address,
            transaction=TransactionRequestEIP1559(to=address, value=0, data=f"0x{root}"),
            network=_chain_id_to_network(chain_id),
        )
    return str(tx_hash).lower(), address.lower()


def _rpc_url(chain_id: int) -> str:
    from teardrop.agent_wallets import _FALLBACK_RPC

    url = get_settings().base_rpc_url or _FALLBACK_RPC.get(chain_id, "")
    if not url:
        raise ValueError("No RPC URL is available for the anchor chain")
    return url


async def _rpc(client: httpx.AsyncClient, url: str, method: str, params: list[Any]) -> Any:
    response = await client.post(url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError("Anchor RPC returned an error")
    return body.get("result")


async def _confirm(client: httpx.AsyncClient, row: Row) -> bool:
    batch_id, tx_hash = str(row["id"]), str(row["tx_hash"])
    url = _rpc_url(int(row["chain_id"]))
    tx = await _rpc(client, url, "eth_getTransactionByHash", [tx_hash])
    if tx is None:
        if row["lease_expired"]:
            await _reset_submission(batch_id, tx_hash, "anchor transaction not found")
        return False
    if tx.get("blockNumber") is None:
        return False
    receipt = await _rpc(client, url, "eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        return False
    matches = (
        str(tx.get("input", "")).lower() == f"0x{row['merkle_root']}"
        and str(tx.get("from", "")).lower() == str(row["anchor_address"])
        and str(tx.get("to", "")).lower() == str(row["anchor_address"])
    )
    if receipt.get("status") != "0x1" or not matches:
        await _reset_submission(batch_id, tx_hash, "anchor transaction reverted or did not match the batch")
        return False
    block = await _rpc(client, url, "eth_getBlockByNumber", [receipt["blockNumber"], False])
    if block is None:
        return False
    finalized = await _rpc(client, url, "eth_getBlockByNumber", ["finalized", False])
    if finalized is None or int(receipt["blockNumber"], 16) > int(finalized["number"], 16):
        return False
    if not receipt.get("blockHash") or receipt["blockHash"] != block.get("hash"):
        return False
    anchored_at = datetime.fromtimestamp(int(block["timestamp"], 16), tz=timezone.utc)
    await _record_confirmation(batch_id, tx_hash, int(receipt["blockNumber"], 16), anchored_at)
    return True


async def anchor_tick() -> int:
    """Seal, send, and confirm commitment batches. Returns the number confirmed."""
    chain_id = anchor_chain_id()
    for _ in range(_MAX_BATCHES_PER_TICK):
        if await seal_batch(chain_id) is None:
            break

    for _ in range(_MAX_BATCHES_PER_TICK):
        batch = await _claim_unsent_batch()
        if batch is None:
            break
        batch_id = str(batch["id"])
        try:
            tx_hash, address = await _send_root(str(batch["merkle_root"]), int(batch["chain_id"]))
            if not (_TX_HASH.fullmatch(tx_hash) and _ADDRESS.fullmatch(address)):
                raise ValueError("CDP returned an unexpected transaction reference")
        except Exception as exc:
            error = f"anchor send failed: {type(exc).__name__}"
            await _release_unsent(batch_id, error)
            logger.warning("vor anchor send failed batch_id=%s error=%s", batch_id, type(exc).__name__)
            break
        await _record_submission(batch_id, tx_hash, address)
        logger.info("vor anchor submitted batch_id=%s tx_hash=%s", batch_id, tx_hash)

    confirmed = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for row in await _list_submitted(_MAX_BATCHES_PER_TICK):
            try:
                if await _confirm(client, row):
                    confirmed += 1
            except Exception as exc:
                logger.warning("vor anchor confirmation failed batch_id=%s error=%s", row["id"], type(exc).__name__)
    return confirmed
