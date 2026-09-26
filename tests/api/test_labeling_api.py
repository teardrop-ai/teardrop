# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


@pytest.mark.anyio
async def test_list_labeling_predictions_is_org_scoped(api_client, monkeypatch):
    rows = [
        {
            "id": "prediction-1",
            "org_id": "test-org-id",
            "source_kind": "scheduled_run",
            "source_id": "run-1",
            "run_id": "run-1",
            "schedule_id": "schedule-1",
            "definition_key": "entry_timing",
            "definition_version": 1,
            "predictions": {"task_class": "entry_timing"},
            "payload_sha256": "a" * 64,
            "prediction_at": _NOW,
            "status": "accepted",
            "parse_error": "",
            "created_at": _NOW,
        }
    ]
    monkeypatch.setattr("teardrop.routers.labeling.list_predictions", AsyncMock(return_value=rows))

    response = await api_client.get("/labeling/predictions")

    assert response.status_code == 200
    assert response.json()["items"][0]["predictions"]["task_class"] == "entry_timing"


@pytest.mark.anyio
async def test_bind_labeling_definition_requires_owned_schedule(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.labeling.get_scheduled_run", AsyncMock(return_value=None))

    response = await api_client.post(
        "/labeling/bindings",
        json={"schedule_id": "schedule-1", "definition_key": "entry_timing", "definition_version": 1},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_bind_labeling_definition(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.labeling.get_scheduled_run",
        AsyncMock(return_value=SimpleNamespace(id="schedule-1", org_id="test-org-id")),
    )
    monkeypatch.setattr(
        "teardrop.routers.labeling.get_definition",
        AsyncMock(return_value=SimpleNamespace(key="entry_timing", version=1)),
    )
    binding = AsyncMock(return_value="binding-1")
    monkeypatch.setattr("teardrop.routers.labeling.create_binding", binding)

    response = await api_client.post(
        "/labeling/bindings",
        json={"schedule_id": "schedule-1", "definition_key": "entry_timing", "definition_version": 1},
    )

    assert response.status_code == 201
    assert response.json()["id"] == "binding-1"
    binding.assert_awaited_once()


@pytest.mark.anyio
async def test_override_labeling_result_appends_external_result(api_client, monkeypatch):
    override = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.labeling.append_result_override", override)

    response = await api_client.post(
        "/labeling/results/target-1/override",
        json={
            "label": "up",
            "score": 1,
            "status": "correct",
            "actual": {"direction": "up"},
            "source": "external",
        },
    )

    assert response.status_code == 201
    assert response.json() == {"status": "recorded"}
    override.assert_awaited_once()


@pytest.mark.anyio
async def test_override_labeling_result_rejects_automatic_source(api_client, monkeypatch):
    response = await api_client.post(
        "/labeling/results/target-1/override",
        json={"label": "up", "score": 1, "status": "correct", "source": "automatic"},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_labeling_requires_auth(anon_client):
    response = await anon_client.get("/labeling/results")
    assert response.status_code == 401


# ─── Verified-outcome submissions and proofs ────────────────────────────────

_ORG = "test-org-id"
_PREDICTIONS = {"task_class": "stablecoin_spread", "prediction": {"next_week_spread_direction": "widen"}}


def _vor_definition():
    from labeling.contracts import Definition

    return Definition(
        key="stablecoin_spread",
        version=1,
        prediction_schema={"type": "object"},
        parser_key="stablecoin_root",
        parser_version="1",
        config={"horizon_seconds": 604800},
    )


def _signed_body(account, *, predictions=None, org_id=_ORG, idempotency_key="key-1"):
    from eth_account.messages import encode_defunct

    from labeling.commitments import prediction_signing_message
    from labeling.contracts import validate_prediction

    predictions = predictions or _PREDICTIONS
    message = prediction_signing_message(
        org_id=org_id,
        definition_key="stablecoin_spread",
        definition_version=1,
        idempotency_key=idempotency_key,
        payload_sha256=validate_prediction(predictions),
    )
    return {
        "definition_key": "stablecoin_spread",
        "definition_version": 1,
        "idempotency_key": idempotency_key,
        "signer_address": account.address,
        "signature": "0x" + bytes(account.sign_message(encode_defunct(text=message)).signature).hex(),
        "predictions": predictions,
    }


@pytest.fixture
def vor_env(monkeypatch):
    from eth_account import Account

    settings = SimpleNamespace(vor_enabled=True, rate_limit_vor_submit_rpm=1000, vor_anchor_interval_seconds=3600)
    monkeypatch.setattr("teardrop.routers.labeling.get_settings", lambda: settings)
    monkeypatch.setattr("teardrop.routers.labeling._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.labeling.get_definition", AsyncMock(return_value=_vor_definition()))
    linked = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.labeling.is_wallet_linked_to_org", linked)
    insert = AsyncMock(return_value=("prediction-1", True))
    monkeypatch.setattr("teardrop.routers.labeling.insert_prediction", insert)
    return SimpleNamespace(settings=settings, account=Account.create(), insert=insert, linked=linked)


@pytest.mark.anyio
async def test_submit_prediction_commits_with_server_timestamp(api_client, vor_env):
    response = await api_client.post("/labeling/predictions", json=_signed_body(vor_env.account))

    assert response.status_code == 201
    assert response.json()["created"] is True
    kwargs = vor_env.insert.await_args.kwargs
    assert kwargs["source_kind"] == "external"
    assert kwargs["source_id"] == "key-1"
    assert kwargs["commit"] is True
    assert kwargs["signer_address"] == vor_env.account.address.lower()
    assert abs((kwargs["prediction_at"] - datetime.now(timezone.utc)).total_seconds()) < 60
    vor_env.linked.assert_awaited_once_with(_ORG, vor_env.account.address.lower())


@pytest.mark.anyio
async def test_submit_prediction_replay_returns_200(api_client, vor_env):
    vor_env.insert.return_value = ("prediction-1", False)

    response = await api_client.post("/labeling/predictions", json=_signed_body(vor_env.account))

    assert response.status_code == 200
    assert response.json()["created"] is False


@pytest.mark.anyio
async def test_submit_prediction_conflict_returns_409(api_client, vor_env):
    from labeling.store import PredictionConflictError

    vor_env.insert.side_effect = PredictionConflictError("conflict")

    response = await api_client.post("/labeling/predictions", json=_signed_body(vor_env.account))

    assert response.status_code == 409


@pytest.mark.anyio
async def test_submit_prediction_rejects_signature_for_other_org(api_client, vor_env):
    from labeling.contracts import validate_prediction

    response = await api_client.post("/labeling/predictions", json=_signed_body(vor_env.account, org_id="other-org"))

    assert response.status_code == 401
    assert validate_prediction(_PREDICTIONS) in response.json()["detail"]
    vor_env.insert.assert_not_awaited()


@pytest.mark.anyio
async def test_submit_prediction_rejects_unlinked_signer(api_client, vor_env):
    vor_env.linked.return_value = False

    response = await api_client.post("/labeling/predictions", json=_signed_body(vor_env.account))

    assert response.status_code == 403
    vor_env.insert.assert_not_awaited()


@pytest.mark.anyio
async def test_submit_prediction_rejects_client_timestamp(api_client, vor_env):
    body = {**_signed_body(vor_env.account), "prediction_at": "2020-01-01T00:00:00Z"}

    response = await api_client.post("/labeling/predictions", json=body)

    assert response.status_code == 422
    vor_env.insert.assert_not_awaited()


@pytest.mark.anyio
async def test_submit_prediction_rejects_horizon_shorter_than_anchor_window(api_client, vor_env):
    vor_env.settings.vor_anchor_interval_seconds = 86400 * 4

    response = await api_client.post("/labeling/predictions", json=_signed_body(vor_env.account))

    assert response.status_code == 422
    vor_env.insert.assert_not_awaited()


@pytest.mark.anyio
async def test_submit_prediction_unknown_definition_returns_404(api_client, vor_env, monkeypatch):
    monkeypatch.setattr("teardrop.routers.labeling.get_definition", AsyncMock(return_value=None))

    response = await api_client.post("/labeling/predictions", json=_signed_body(vor_env.account))

    assert response.status_code == 404


@pytest.mark.anyio
async def test_submit_prediction_disabled_returns_404(api_client, vor_env):
    vor_env.settings.vor_enabled = False

    response = await api_client.post("/labeling/predictions", json=_signed_body(vor_env.account))

    assert response.status_code == 404
    vor_env.insert.assert_not_awaited()


def _commitment_row(*, anchored: bool, batch: bool = True):
    from labeling.commitments import LEAF_VERSION_PREDICTION, leaf_hash, new_salt, prediction_leaf_fields

    salt = new_salt()
    fields = prediction_leaf_fields(
        prediction_id="prediction-1",
        org_id=_ORG,
        signer_address="0x" + "ab" * 20,
        definition_key="stablecoin_spread",
        definition_version=1,
        payload_sha256="a" * 64,
        prediction_at=_NOW,
    )
    leaf = leaf_hash(LEAF_VERSION_PREDICTION, fields, salt)
    leaves = ["1" * 64, leaf, "2" * 64]
    from labeling.commitments import merkle_root

    row = {
        "id": "prediction-1",
        "org_id": _ORG,
        "signer_address": "0x" + "ab" * 20,
        "definition_key": "stablecoin_spread",
        "definition_version": 1,
        "payload_sha256": "a" * 64,
        "prediction_at": _NOW,
        "commit_salt": salt,
        "leaf_sha256": leaf,
        "anchor_batch_id": "batch-1" if batch else None,
        "anchor_leaf_index": 1 if batch else None,
        "merkle_root": merkle_root(leaves) if batch else None,
        "leaf_count": 3 if batch else None,
        "chain_id": 8453 if batch else None,
        "tx_hash": "0x" + "cd" * 32 if anchored else None,
        "anchor_address": "0x" + "ef" * 20 if anchored else None,
        "block_number": 16 if anchored else None,
        "anchored_at": _NOW if anchored else None,
    }
    return row, leaves


@pytest.mark.anyio
async def test_prediction_proof_verifies_against_anchored_root(api_client, monkeypatch):
    from labeling.commitments import verify_path

    row, leaves = _commitment_row(anchored=True)
    monkeypatch.setattr("teardrop.routers.labeling.get_prediction_commitment", AsyncMock(return_value=row))
    monkeypatch.setattr("teardrop.routers.labeling.batch_leaves", AsyncMock(return_value=leaves))

    response = await api_client.get("/labeling/predictions/prediction-1/proof")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "anchored"
    anchor = body["anchor"]
    assert verify_path(
        body["leaf_sha256"], anchor["leaf_index"], anchor["tree_size"], anchor["audit_path"], anchor["merkle_root"]
    )


@pytest.mark.anyio
async def test_prediction_proof_pending_has_no_anchor(api_client, monkeypatch):
    row, _ = _commitment_row(anchored=False, batch=False)
    monkeypatch.setattr("teardrop.routers.labeling.get_prediction_commitment", AsyncMock(return_value=row))

    response = await api_client.get("/labeling/predictions/prediction-1/proof")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["anchor"] is None


@pytest.mark.anyio
async def test_prediction_proof_is_org_scoped(api_client, monkeypatch):
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.labeling.get_prediction_commitment", lookup)

    response = await api_client.get("/labeling/predictions/prediction-1/proof")

    assert response.status_code == 404
    lookup.assert_awaited_once_with(_ORG, "prediction-1")


@pytest.mark.anyio
async def test_prediction_proof_fails_closed_on_tampered_leaf(api_client, monkeypatch):
    row, _ = _commitment_row(anchored=False, batch=False)
    row["commit_salt"] = "0" * 64
    monkeypatch.setattr("teardrop.routers.labeling.get_prediction_commitment", AsyncMock(return_value=row))

    response = await api_client.get("/labeling/predictions/prediction-1/proof")

    assert response.status_code == 500
    assert "0" * 64 not in response.text
