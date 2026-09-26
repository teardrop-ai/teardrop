# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Tamper-evident commitments: versioned leaves, RFC 6962 Merkle trees, and EIP-191 signer recovery.

Anchorable-table contract
-------------------------
Any table can be anchored by the single worker in ``labeling.anchor`` when it has:

* ``leaf_sha256`` and ``commit_salt``: 64-char lowercase hex, written together at insert.
* ``anchor_batch_id`` (FK to ``commitment_batches``) and ``anchor_leaf_index``: written
  together, exactly once, by the seal step.
* A guard trigger that rejects DELETE of committed rows, changes to any committed column,
  and reassignment of a non-null batch.

The table is then registered with ``labeling.anchor.register_anchor_source``.

Leaf versions: ``1`` = prediction. ``2`` is reserved for execution receipts.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from labeling.contracts import canonical_json, utc_datetime

LEAF_VERSION_PREDICTION = 1
LEAF_VERSION_EXECUTION_RECEIPT = 2
SUPPORTED_LEAF_VERSIONS = frozenset({LEAF_VERSION_PREDICTION})
HASH_ALGORITHM = "rfc6962-sha256"
MAX_BATCH_LEAVES = 4096
PREDICTION_SIGNING_DOMAIN = "Teardrop VOR prediction v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_LEAF_KEYS = frozenset({"v", "salt"})


def new_salt() -> str:
    return secrets.token_hex(32)


def leaf_hash(version: int, fields: Mapping[str, Any], salt: str) -> str:
    """Return ``sha256(0x00 || canonical_json({...fields, v, salt}))`` as lowercase hex."""
    if version not in SUPPORTED_LEAF_VERSIONS:
        raise ValueError("Unsupported commitment leaf version")
    if not _HEX64.fullmatch(salt):
        raise ValueError("Commitment salt must be 64 lowercase hex characters")
    if _RESERVED_LEAF_KEYS & set(fields):
        raise ValueError("Commitment fields cannot override reserved keys")
    preimage = canonical_json({**fields, "v": version, "salt": salt})
    return hashlib.sha256(b"\x00" + preimage.encode("ascii")).hexdigest()


def prediction_leaf_fields(
    *,
    prediction_id: str,
    org_id: str,
    signer_address: str | None,
    definition_key: str,
    definition_version: int,
    payload_sha256: str,
    prediction_at: datetime,
) -> dict[str, Any]:
    return {
        "prediction_id": prediction_id,
        "org_id": org_id,
        "signer": signer_address or "",
        "definition": f"{definition_key}@{definition_version}",
        "payload_sha256": payload_sha256,
        "prediction_at": utc_datetime(prediction_at).isoformat(timespec="microseconds"),
    }


def _node(left: str, right: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def _split(size: int) -> int:
    return 1 << ((size - 1).bit_length() - 1)


def merkle_root(leaves: Sequence[str]) -> str:
    if not leaves:
        raise ValueError("A Merkle tree requires at least one leaf")
    if len(leaves) == 1:
        return leaves[0]
    k = _split(len(leaves))
    return _node(merkle_root(leaves[:k]), merkle_root(leaves[k:]))


def audit_path(leaves: Sequence[str], index: int) -> list[str]:
    if not 0 <= index < len(leaves):
        raise IndexError("Leaf index is outside the tree")
    if len(leaves) == 1:
        return []
    k = _split(len(leaves))
    if index < k:
        return audit_path(leaves[:k], index) + [merkle_root(leaves[k:])]
    return audit_path(leaves[k:], index - k) + [merkle_root(leaves[:k])]


def verify_path(leaf: str, index: int, tree_size: int, path: Sequence[str], root: str) -> bool:
    """Verify an inclusion proof per RFC 9162 section 2.1.3.2."""
    if not 0 <= index < tree_size:
        return False
    fn, sn, result = index, tree_size - 1, leaf
    try:
        for sibling in path:
            if sn == 0:
                return False
            if fn & 1 or fn == sn:
                result = _node(sibling, result)
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
            else:
                result = _node(result, sibling)
            fn >>= 1
            sn >>= 1
    except ValueError:
        return False
    return sn == 0 and result == root


def prediction_signing_message(
    *,
    org_id: str,
    definition_key: str,
    definition_version: int,
    idempotency_key: str,
    payload_sha256: str,
) -> str:
    return "\n".join(
        (
            PREDICTION_SIGNING_DOMAIN,
            f"org:{org_id}",
            f"definition:{definition_key}@{definition_version}",
            f"idempotency_key:{idempotency_key}",
            f"payload_sha256:{payload_sha256}",
        )
    )


def recover_signer(message: str, signature: str) -> str | None:
    """Return the lowercase EIP-191 signer address, or None when the signature is malformed."""
    try:
        return str(Account.recover_message(encode_defunct(text=message), signature=signature)).lower()
    except Exception:
        return None
