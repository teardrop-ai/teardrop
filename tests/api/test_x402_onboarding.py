# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""API tests for payment-first x402 org bootstrap."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from x402.schemas import PaymentRequirements

import billing
from billing.models import BillingResult
from teardrop.onboarding import bootstrap_org_from_payment
from teardrop.users import Org, OrgClientCredential, User
from teardrop.wallets import Wallet, WalletProvisioningResult


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch):
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))


@pytest.fixture
def bootstrap_context(monkeypatch, test_settings):
    test_settings.billing_enabled = True
    test_settings.machine_provisioning_enabled = True
    test_settings.x402_onboarding_enabled = True
    test_settings.x402_scheme = "exact"
    test_settings.credit_min_run_reserve_usdc = 50_000
    monkeypatch.setattr("teardrop.routers.auth.settings", test_settings)
    monkeypatch.setattr("teardrop.onboarding.settings", test_settings, raising=False)
    monkeypatch.setattr("teardrop.onboarding.get_provisioning_state_by_payment_ref", AsyncMock(return_value=None))
    requirement = PaymentRequirements(
        scheme="exact",
        network="eip155:84532",
        asset="0x0000000000000000000000000000000000000000",
        amount="50000",
        pay_to="0x0000000000000000000000000000000000000001",
        max_timeout_seconds=300,
    )
    requirements_mock = AsyncMock(return_value=(50_000, [requirement]))
    monkeypatch.setattr("teardrop.onboarding.get_bootstrap_payment_requirements", requirements_mock)
    return requirement, requirements_mock


def _provisioned(address: str) -> WalletProvisioningResult:
    now = datetime.now(timezone.utc)
    org = Org(id="org-x402", name=f"wallet-{address.lower()}", slug="wallet-x402", acquisition_source="x402", created_at=now)
    user = User(
        id="user-x402",
        email=f"{address.lower()}@wallet",
        org_id=org.id,
        hashed_secret="hash",
        salt="salt",
        role="user",
        is_active=True,
        is_verified=True,
        created_at=now,
    )
    wallet = Wallet(
        id="wallet-x402",
        address=address,
        chain_id=84532,
        user_id=user.id,
        org_id=org.id,
        is_primary=True,
        created_at=now,
    )
    return WalletProvisioningResult(org=org, user=user, wallet=wallet, created=True)


def _verified(requirement, address: str = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045") -> BillingResult:
    return BillingResult(verified=True, payment_requirements=requirement, payer=address, scheme="exact")


@pytest.mark.anyio
async def test_x402_bootstrap_without_header_returns_payment_requirements(anon_client, bootstrap_context, monkeypatch):
    _, requirements_mock = bootstrap_context
    rate_limit_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.auth._enforce_rate_limit", rate_limit_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"})

    assert response.status_code == 402
    body = response.json()
    assert body["accepts"][0]["amount"] == "50000"
    assert body["resource"]["url"] == "http://test/token"
    assert "payment-required" in response.headers
    requirements_mock.assert_awaited_once()
    rate_limit_mock.assert_awaited_once()
    assert rate_limit_mock.call_args.args[0].startswith("auth:")


@pytest.mark.anyio
async def test_x402_bootstrap_rate_limits_verified_payer_and_releases_claim(bootstrap_context, monkeypatch, test_settings):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    monkeypatch.setattr(billing, "verify_payment", AsyncMock(return_value=_verified(bootstrap_context[0], address)))
    # New wallet (no existing wallet) so the limiter branch is reached.
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(return_value=None))
    rate_limit_mock = AsyncMock(side_effect=HTTPException(status_code=429, detail="rate limited"))
    release_mock = AsyncMock()
    monkeypatch.setattr("teardrop.onboarding._enforce_rate_limit", rate_limit_mock)
    monkeypatch.setattr(billing, "release_payment_nonce", release_mock)

    with pytest.raises(HTTPException) as error:
        await bootstrap_org_from_payment("payment", None)

    assert error.value.status_code == 429
    assert rate_limit_mock.call_args.args[0] == f"provision:addr:{address.lower()}"
    assert rate_limit_mock.call_args.args[1] == test_settings.rate_limit_org_provision_rpm
    release_mock.assert_awaited_once_with("payment")


@pytest.mark.anyio
async def test_x402_bootstrap_flag_off_does_not_touch_payment(anon_client, bootstrap_context, monkeypatch, test_settings):
    test_settings.x402_onboarding_enabled = False
    verify_mock = AsyncMock()
    monkeypatch.setattr(billing, "verify_payment", verify_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 404
    verify_mock.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("disabled_flag", ["billing_enabled", "machine_provisioning_enabled"])
async def test_x402_bootstrap_requires_all_enabling_flags(
    anon_client,
    bootstrap_context,
    monkeypatch,
    test_settings,
    disabled_flag,
):
    setattr(test_settings, disabled_flag, False)
    verify_mock = AsyncMock()
    monkeypatch.setattr(billing, "verify_payment", verify_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 404
    verify_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x402_bootstrap_rejects_ambiguous_credentials(anon_client, bootstrap_context):
    response = await anon_client.post(
        "/token",
        json={"grant_type": "x402", "client_id": "unexpected"},
    )

    assert response.status_code == 400


@pytest.mark.anyio
async def test_x402_bootstrap_invalid_payment_returns_protocol_402(anon_client, bootstrap_context, monkeypatch):
    requirement, _ = bootstrap_context
    verify_mock = AsyncMock(return_value=BillingResult(error="internal verification detail"))
    monkeypatch.setattr(billing, "verify_payment", verify_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "invalid"})

    assert response.status_code == 402
    assert response.json()["error"] == "Payment verification failed"
    assert "internal verification detail" not in response.text
    assert response.json()["accepts"][0]["amount"] == requirement.amount


@pytest.mark.anyio
async def test_x402_bootstrap_missing_payer_fails_closed(anon_client, bootstrap_context, monkeypatch):
    verify_mock = AsyncMock(return_value=BillingResult(verified=True, payment_requirements=bootstrap_context[0], payer=""))
    provision_mock = AsyncMock()
    monkeypatch.setattr(billing, "verify_payment", verify_mock)
    monkeypatch.setattr("teardrop.onboarding.provision_org_for_wallet", provision_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 402
    assert response.json()["error"] == "Payment verified but payer identity unavailable"
    provision_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x402_bootstrap_existing_wallet_returns_409(anon_client, bootstrap_context, monkeypatch):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    verify_mock = AsyncMock(return_value=_verified(bootstrap_context[0], address))
    existing = _provisioned(address).wallet
    monkeypatch.setattr(billing, "verify_payment", verify_mock)
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(return_value=existing))
    monkeypatch.setattr(
        "teardrop.onboarding.get_org_by_id",
        AsyncMock(
            return_value=Org(
                id=existing.org_id,
                name="human-org",
                slug="human-org",
                acquisition_source="email",
                created_at=datetime.now(timezone.utc),
            )
        ),
    )
    monkeypatch.setattr("teardrop.onboarding.get_provisioning_state_by_payment_ref", AsyncMock(return_value=None))
    provision_mock = AsyncMock()
    monkeypatch.setattr("teardrop.onboarding.provision_org_for_wallet", provision_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 409
    provision_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x402_bootstrap_human_owned_wallet_returns_409(anon_client, bootstrap_context, monkeypatch):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    existing = _provisioned(address).wallet
    human_org = Org(
        id=existing.org_id,
        name="human-org",
        slug="human-org",
        acquisition_source="email",
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(billing, "verify_payment", AsyncMock(return_value=_verified(bootstrap_context[0], address)))
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(return_value=existing))
    monkeypatch.setattr("teardrop.onboarding.get_org_by_id", AsyncMock(return_value=human_org))
    monkeypatch.setattr("teardrop.onboarding.provision_org_for_wallet", AsyncMock())

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 409


@pytest.mark.anyio
async def test_x402_bootstrap_new_payment_uses_existing_machine_org(anon_client, bootstrap_context, monkeypatch):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    provisioned = _provisioned(address)
    credential = OrgClientCredential(
        client_id="client-existing-machine",
        org_id=provisioned.org.id,
        hashed_secret="hash",
        salt="salt",
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(billing, "verify_payment", AsyncMock(return_value=_verified(bootstrap_context[0], address)))
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(return_value=provisioned.wallet))
    monkeypatch.setattr("teardrop.onboarding.get_org_by_id", AsyncMock(return_value=provisioned.org))
    monkeypatch.setattr("teardrop.onboarding.get_provisioning_state_by_payment_ref", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "teardrop.onboarding.provision_org_for_wallet",
        AsyncMock(return_value=provisioned.model_copy(update={"created": False})),
    )
    monkeypatch.setattr(
        billing,
        "settle_payment",
        AsyncMock(
            return_value=BillingResult(
                verified=True,
                settled=True,
                tx_hash="0xnew-payment",
                amount_usdc=50_000,
                payment_requirements=bootstrap_context[0],
            )
        ),
    )
    monkeypatch.setattr(
        billing,
        "record_onboarding_settlement",
        AsyncMock(return_value=(credential, None)),
    )
    # Repeat top-ups into an existing machine org are economically gated and
    # must NOT consume the new-org provisioning bucket.
    address_rate_limit_mock = AsyncMock()
    monkeypatch.setattr("teardrop.onboarding._enforce_rate_limit", address_rate_limit_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "new-payment"})

    assert response.status_code == 200
    assert response.json()["org_id"] == provisioned.org.id
    assert response.json()["client_id"] == credential.client_id
    assert "client_secret" not in response.json()
    address_rate_limit_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x402_bootstrap_settlement_failure_never_credits(anon_client, bootstrap_context, monkeypatch):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    verify_mock = AsyncMock(return_value=_verified(bootstrap_context[0], address))
    provision_mock = AsyncMock(return_value=_provisioned(address))
    settle_mock = AsyncMock(
        return_value=BillingResult(
            verified=True,
            settled=False,
            error="Settlement rejected by facilitator",
            payment_requirements=bootstrap_context[0],
        )
    )
    credit_mock = AsyncMock()
    audit_mock = AsyncMock()
    monkeypatch.setattr(billing, "verify_payment", verify_mock)
    monkeypatch.setattr(billing, "settle_payment", settle_mock)
    monkeypatch.setattr(billing, "record_onboarding_settlement", credit_mock)
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.onboarding.provision_org_for_wallet", provision_mock)
    monkeypatch.setattr("teardrop.onboarding._record_settlement_event", audit_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 402
    credit_mock.assert_not_awaited()
    audit_mock.assert_awaited_once()
    assert audit_mock.call_args.args[-2] == "failed"


@pytest.mark.anyio
async def test_x402_bootstrap_ambiguous_settlement_keeps_replay_claim(anon_client, bootstrap_context, monkeypatch):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    monkeypatch.setattr(billing, "verify_payment", AsyncMock(return_value=_verified(bootstrap_context[0], address)))
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.onboarding.provision_org_for_wallet", AsyncMock(return_value=_provisioned(address)))
    monkeypatch.setattr(
        billing,
        "settle_payment",
        AsyncMock(
            return_value=BillingResult(
                verified=True,
                settled=False,
                error="Settlement failed: facilitator timeout",
                payment_requirements=bootstrap_context[0],
            )
        ),
    )
    audit_mock = AsyncMock(return_value=True)
    release_mock = AsyncMock()
    monkeypatch.setattr("teardrop.onboarding._record_settlement_event", audit_mock)
    monkeypatch.setattr(billing, "release_payment_nonce", release_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 402
    assert audit_mock.call_args.args[-2] == "ambiguous"
    release_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x402_bootstrap_pending_existing_machine_payment_is_not_retried(anon_client, bootstrap_context, monkeypatch):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    provisioned = _provisioned(address)
    monkeypatch.setattr(billing, "verify_payment", AsyncMock(return_value=_verified(bootstrap_context[0], address)))
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(return_value=provisioned.wallet))
    monkeypatch.setattr("teardrop.onboarding.get_org_by_id", AsyncMock(return_value=provisioned.org))
    monkeypatch.setattr(
        "teardrop.onboarding.get_provisioning_state_by_payment_ref",
        AsyncMock(
            return_value={
                "provisioned": {
                    "org_id": provisioned.org.id,
                    "method": "x402",
                    "chain_id": 84532,
                    "payer_address": address,
                    "amount_usdc": 50_000,
                    "payment_ref": f"x402:{__import__('hashlib').sha256(b'payment').hexdigest()}",
                },
                "latest_settlement": None,
            }
        ),
    )
    release_mock = AsyncMock()
    settle_mock = AsyncMock()
    monkeypatch.setattr(billing, "release_payment_nonce", release_mock)
    monkeypatch.setattr(billing, "settle_payment", settle_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 409
    settle_mock.assert_not_awaited()
    release_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x402_bootstrap_happy_path_returns_one_time_client_credential(anon_client, bootstrap_context, monkeypatch, caplog):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    provisioned = _provisioned(address)
    credential = OrgClientCredential(
        client_id="client-x402",
        org_id=provisioned.org.id,
        hashed_secret="hash",
        salt="salt",
        created_at=datetime.now(timezone.utc),
    )
    verify_mock = AsyncMock(return_value=_verified(bootstrap_context[0], address))
    settle_mock = AsyncMock(
        return_value=BillingResult(
            verified=True,
            settled=True,
            tx_hash="0xsettlement",
            amount_usdc=50_000,
            payment_requirements=bootstrap_context[0],
        )
    )
    audit_mock = AsyncMock()
    monkeypatch.setattr(billing, "verify_payment", verify_mock)
    monkeypatch.setattr(billing, "settle_payment", settle_mock)
    settlement_mock = AsyncMock(return_value=(credential, "one-time-client-secret"))
    monkeypatch.setattr(billing, "record_onboarding_settlement", settlement_mock)
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.onboarding.provision_org_for_wallet", AsyncMock(return_value=provisioned))
    monkeypatch.setattr("teardrop.onboarding._record_settlement_event", audit_mock)
    auth_rate_limit_mock = AsyncMock()
    address_rate_limit_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.auth._enforce_rate_limit", auth_rate_limit_mock)
    monkeypatch.setattr("teardrop.onboarding._enforce_rate_limit", address_rate_limit_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "payment"})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1_800
    assert body["org_id"] == "org-x402"
    assert body["client_id"] == "client-x402"
    assert body["client_secret"] == "one-time-client-secret"
    auth_rate_limit_mock.assert_awaited_once()
    assert auth_rate_limit_mock.call_args.args[0].startswith("auth:")
    address_rate_limit_mock.assert_awaited_once()
    assert address_rate_limit_mock.call_args.args[0] == f"provision:addr:{address.lower()}"
    from teardrop.auth import decode_access_token

    claims = decode_access_token(body["access_token"])
    assert claims["sub"] == "client-x402"
    assert claims["org_id"] == "org-x402"
    assert claims["auth_method"] == "client_credentials"
    settlement_mock.assert_awaited_once_with(
        "org-x402",
        50_000,
        f"x402:{__import__('hashlib').sha256(b'payment').hexdigest()}",
        address,
        84532,
        "0xsettlement",
    )
    assert "one-time-client-secret" not in caplog.text


@pytest.mark.anyio
async def test_x402_bootstrap_replayed_payment_is_rejected(anon_client, bootstrap_context, monkeypatch):
    replay_error = "Payment already used. Sign a new payment authorization."
    verify_mock = AsyncMock(return_value=BillingResult(error=replay_error))
    monkeypatch.setattr(billing, "verify_payment", verify_mock)

    response = await anon_client.post("/token", json={"grant_type": "x402"}, headers={"X-Payment": "replayed"})

    assert response.status_code == 402
    assert response.json()["error"] == "Payment verification failed"
    verify_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_x402_failed_settlement_can_retry_same_provisioning(anon_client, bootstrap_context, monkeypatch):
    address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    provisioned = _provisioned(address)
    recovered = provisioned.model_copy(update={"created": False})
    payment_ref = f"x402:{__import__('hashlib').sha256(b'payment').hexdigest()}"
    recovery_state = {
        "provisioned": {
            "org_id": provisioned.org.id,
            "method": "x402",
            "chain_id": 84532,
            "payer_address": address,
            "amount_usdc": 50_000,
        },
        "latest_settlement": {"settlement_status": "failed"},
    }
    verify_mock = AsyncMock(side_effect=[_verified(bootstrap_context[0], address), _verified(bootstrap_context[0], address)])
    settle_mock = AsyncMock(
        side_effect=[
            BillingResult(
                verified=True,
                settled=False,
                error="Settlement rejected by facilitator",
                payment_requirements=bootstrap_context[0],
            ),
            BillingResult(
                verified=True,
                settled=True,
                tx_hash="0xretry-settlement",
                amount_usdc=50_000,
                payment_requirements=bootstrap_context[0],
            ),
        ]
    )
    release_mock = AsyncMock()
    audit_mock = AsyncMock()
    credential = OrgClientCredential(
        client_id="client-retry",
        org_id=provisioned.org.id,
        hashed_secret="hash",
        salt="salt",
        created_at=datetime.now(timezone.utc),
    )
    credit_mock = AsyncMock(return_value=(credential, "retry-secret"))
    monkeypatch.setattr(billing, "verify_payment", verify_mock)
    monkeypatch.setattr(billing, "settle_payment", settle_mock)
    monkeypatch.setattr(billing, "release_payment_nonce", release_mock)
    monkeypatch.setattr(billing, "record_onboarding_settlement", credit_mock)
    monkeypatch.setattr("teardrop.onboarding.get_wallet_by_address_any_chain", AsyncMock(side_effect=[None, provisioned.wallet]))
    monkeypatch.setattr("teardrop.onboarding.get_org_by_id", AsyncMock(return_value=provisioned.org))
    monkeypatch.setattr("teardrop.onboarding.get_provisioning_state_by_payment_ref", AsyncMock(return_value=recovery_state))
    monkeypatch.setattr("teardrop.onboarding.provision_org_for_wallet", AsyncMock(side_effect=[provisioned, recovered]))
    monkeypatch.setattr("teardrop.onboarding._record_settlement_event", audit_mock)

    def token_mock(**kwargs):
        return "retry-token"

    monkeypatch.setattr("teardrop.onboarding.create_access_token", token_mock)

    with pytest.raises(HTTPException) as first_error:
        await bootstrap_org_from_payment("payment", None)
    assert first_error.value.status_code == 402

    result = await bootstrap_org_from_payment("payment", None)

    assert result["client_id"] == "client-retry"
    release_mock.assert_awaited_once_with("payment")
    credit_mock.assert_awaited_once_with(
        provisioned.org.id,
        50_000,
        payment_ref,
        address,
        84532,
        "0xretry-settlement",
    )
