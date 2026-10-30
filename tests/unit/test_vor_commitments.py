# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from labeling import commitments
from labeling.commitments import (
    EXECUTION_RECEIPT_KINDS,
    LEAF_VERSION_EXECUTION_RECEIPT,
    LEAF_VERSION_PREDICTION,
    audit_path,
    execution_receipt_leaf_fields,
    leaf_hash,
    merkle_root,
    new_salt,
    prediction_leaf_fields,
    prediction_signing_message,
    receipt_content_sha256,
    recover_signer,
    verify_path,
)


def _leaves(count: int) -> list[str]:
    return [hashlib.sha256(f"leaf-{index}".encode()).hexdigest() for index in range(count)]


def _node(left: str, right: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def test_merkle_root_matches_rfc6962_shape_for_small_trees():
    a, b, c = _leaves(3)
    assert merkle_root([a]) == a
    assert merkle_root([a, b]) == _node(a, b)
    assert merkle_root([a, b, c]) == _node(_node(a, b), c)


def test_merkle_root_rejects_empty_tree():
    with pytest.raises(ValueError):
        merkle_root([])


@pytest.mark.parametrize("size", range(1, 18))
def test_every_audit_path_verifies(size):
    leaves = _leaves(size)
    root = merkle_root(leaves)
    for index, leaf in enumerate(leaves):
        path = audit_path(leaves, index)
        assert verify_path(leaf, index, size, path, root)
        assert not verify_path(_leaves(size + 1)[-1], index, size, path, root)
        if size > 1:
            assert not verify_path(leaf, (index + 1) % size, size, path, root)


def test_verify_path_rejects_out_of_range_and_malformed_inputs():
    leaves = _leaves(4)
    root = merkle_root(leaves)
    path = audit_path(leaves, 0)
    assert not verify_path(leaves[0], 4, 4, path, root)
    assert not verify_path(leaves[0], 0, 4, path + [leaves[1]], root)
    assert not verify_path(leaves[0], 0, 4, ["zz"] + path[1:], root)
    with pytest.raises(IndexError):
        audit_path(leaves, 4)


def test_leaf_hash_is_domain_separated_and_salted():
    fields = {"prediction_id": "p-1", "payload_sha256": "a" * 64}
    salt = new_salt()
    leaf = leaf_hash(LEAF_VERSION_PREDICTION, fields, salt)
    expected_preimage = commitments.canonical_json({**fields, "v": 1, "salt": salt})
    assert leaf == hashlib.sha256(b"\x00" + expected_preimage.encode()).hexdigest()
    assert leaf_hash(LEAF_VERSION_PREDICTION, fields, salt) == leaf
    assert leaf_hash(LEAF_VERSION_PREDICTION, fields, new_salt()) != leaf


def test_leaf_hash_rejects_reserved_version_keys_and_bad_salt():
    with pytest.raises(ValueError):
        leaf_hash(3, {}, new_salt())
    with pytest.raises(ValueError):
        leaf_hash(LEAF_VERSION_PREDICTION, {"v": 9}, new_salt())
    with pytest.raises(ValueError):
        leaf_hash(LEAF_VERSION_PREDICTION, {}, "A" * 64)


def _receipt_fields(**overrides):
    values = {
        "kind": "simulation",
        "receipt_id": "r-1",
        "org_id": "org-1",
        "producer": "simulate_transaction",
        "content_sha256": "b" * 64,
        "created_at": datetime(2026, 10, 30, 14, 0, 0, 7, tzinfo=timezone(timedelta(hours=2))),
    }
    values.update(overrides)
    return execution_receipt_leaf_fields(**values)


def test_execution_receipt_leaf_is_version_2_and_distinct_from_prediction():
    fields = _receipt_fields()
    assert fields == {
        "kind": "simulation",
        "receipt_id": "r-1",
        "org_id": "org-1",
        "producer": "simulate_transaction",
        "content_sha256": "b" * 64,
        "created_at": "2026-10-30T12:00:00.000007+00:00",
    }
    salt = new_salt()
    leaf = leaf_hash(LEAF_VERSION_EXECUTION_RECEIPT, fields, salt)
    expected_preimage = commitments.canonical_json({**fields, "v": 2, "salt": salt})
    assert leaf == hashlib.sha256(b"\x00" + expected_preimage.encode()).hexdigest()
    assert leaf != leaf_hash(LEAF_VERSION_PREDICTION, fields, salt)


@pytest.mark.parametrize("kind", sorted(EXECUTION_RECEIPT_KINDS))
def test_every_receipt_kind_shares_leaf_version_2(kind):
    leaf_hash(LEAF_VERSION_EXECUTION_RECEIPT, _receipt_fields(kind=kind), new_salt())


@pytest.mark.parametrize(
    "overrides",
    [{"kind": "prediction"}, {"content_sha256": "B" * 64}, {"content_sha256": "b" * 63}, {"receipt_id": ""}, {"producer": ""}],
)
def test_execution_receipt_leaf_fields_reject_invalid_values(overrides):
    with pytest.raises(ValueError):
        _receipt_fields(**overrides)


def test_receipt_content_sha256_is_canonical():
    assert receipt_content_sha256({"b": 1, "a": [2]}) == receipt_content_sha256({"a": [2], "b": 1})
    assert receipt_content_sha256({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()
    with pytest.raises(ValueError):
        receipt_content_sha256({"x": float("nan")})


def test_prediction_leaf_fields_normalize_timestamp_to_utc_microseconds():
    local = datetime(2026, 9, 26, 14, 0, 0, 5, tzinfo=timezone(timedelta(hours=2)))
    fields = prediction_leaf_fields(
        prediction_id="p-1",
        org_id="org-1",
        signer_address=None,
        definition_key="entry_timing",
        definition_version=1,
        payload_sha256="a" * 64,
        prediction_at=local,
    )
    assert fields["prediction_at"] == "2026-09-26T12:00:00.000005+00:00"
    assert fields["signer"] == ""
    assert fields["definition"] == "entry_timing@1"


def test_signature_round_trip_binds_org():
    account = Account.create()
    message = prediction_signing_message(
        org_id="org-1",
        definition_key="entry_timing",
        definition_version=1,
        idempotency_key="key-1",
        payload_sha256="a" * 64,
    )
    signature = "0x" + bytes(account.sign_message(encode_defunct(text=message)).signature).hex()
    assert recover_signer(message, signature) == account.address.lower()

    other_org = message.replace("org:org-1", "org:org-2")
    assert recover_signer(other_org, signature) != account.address.lower()


def test_recover_signer_returns_none_for_malformed_signature():
    assert recover_signer("message", "0x" + "00" * 65) is None
