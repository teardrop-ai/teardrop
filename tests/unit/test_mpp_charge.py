# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for MPP evm charge verification (billing/mpp.py)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from billing.mpp import (
    MPP_MALFORMED_CREDENTIAL,
    MPP_VERIFY_FAILED,
    MppRpcError,
    build_mpp_challenge,
    encode_receipt,
    mpp_configured,
    mpp_nonce_key,
    mpp_www_authenticate,
    parse_payment_authorization,
    receipt_matches,
    validate_mpp_credential,
    verify_mpp_payment,
)

_RECIPIENT = "0x" + "ab" * 20
_CURRENCY = "0x" + "cd" * 20
_PAYER = "0x" + "42" * 20
_TX = "0x" + "11" * 32
_COST = 10_000
_SECRET = "s" * 32


def _settings(**overrides):
    base = dict(
        mpp_enabled=True,
        mcp_auth_enabled=True,
        mcp_x402_enabled=True,
        mcp_billing_enabled=True,
        mpp_recipient=_RECIPIENT,
        mpp_currency=_CURRENCY,
        mpp_secret_key=_SECRET,
        base_rpc_url="http://rpc.test",
        mpp_challenge_ttl_seconds=300,
        x402_network="eip155:8453",
        app_host="teardrop.test",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _credential(settings, *, challenge=None, tx=_TX, source=f"did:pkh:eip155:8453:{_PAYER}"):
    challenge = challenge or build_mpp_challenge(tool_cost_usdc=_COST, settings=settings)
    return {"challenge": challenge, "source": source, "payload": {"type": "hash", "hash": tx}}


def _b64(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


class TestChallenge:
    def test_shape_binding_and_uniqueness(self):
        settings = _settings()
        first = build_mpp_challenge(tool_cost_usdc=_COST, settings=settings)
        second = build_mpp_challenge(tool_cost_usdc=_COST, settings=settings)
        assert (first["method"], first["intent"], first["realm"]) == ("evm", "charge", "teardrop.test")
        request = first["request"]
        assert (request["amount"], request["currency"], request["recipient"]) == (str(_COST), _CURRENCY, _RECIPIENT)
        # draft-evm-charge-00 §4.2: chainId REQUIRED; only hash credentials are advertised.
        assert request["methodDetails"]["chainId"] == 8453
        assert request["methodDetails"]["credentialTypes"] == ["hash"]
        assert isinstance(first["opaque"]["iat"], int)
        assert first["id"] != second["id"]

    def test_configured_requires_every_dependency(self):
        assert mpp_configured(_settings()) is True
        for override in (
            {"mpp_enabled": False},
            {"mcp_auth_enabled": False},
            {"mcp_x402_enabled": False},
            {"mcp_billing_enabled": False},
            {"mpp_recipient": ""},
            {"mpp_currency": ""},
            {"base_rpc_url": ""},
            {"mpp_secret_key": "short"},
            {"x402_network": "solana:mainnet"},
        ):
            assert mpp_configured(_settings(**override)) is False, override

    def test_nonce_key_normalizes(self):
        assert mpp_nonce_key(" 0xABCDEF ") == "mpp:0xabcdef"

    def test_www_authenticate_round_trips_through_validation(self):
        """HTTP clients echo request/opaque as base64url-JCS strings; binding must still verify."""
        settings = _settings()
        challenge = build_mpp_challenge(tool_cost_usdc=_COST, settings=settings)
        header = mpp_www_authenticate(challenge)
        assert header.startswith("Payment ") and f'id="{challenge["id"]}"' in header
        params = dict(part.split("=", 1) for part in header.removeprefix("Payment ").split(", "))
        echoed = {key: value.strip('"') for key, value in params.items()}
        parsed, code, _ = validate_mpp_credential(
            _credential(settings, challenge=echoed), tool_cost_usdc=_COST, settings=settings
        )
        assert parsed is not None and code is None


class TestValidate:
    def test_happy_returns_verified_payer(self):
        settings = _settings()
        parsed, code, msg = validate_mpp_credential(_credential(settings), tool_cost_usdc=_COST, settings=settings)
        assert code is None and msg is None
        assert parsed.payer == _PAYER and parsed.tx_hash == _TX

    def test_malformed_shapes(self):
        settings = _settings()
        for bad in (None, "x", [], {}, {"challenge": {}, "source": "s", "payload": {}}):
            parsed, code, _ = validate_mpp_credential(bad, tool_cost_usdc=_COST, settings=settings)
            assert parsed is None and code == MPP_MALFORMED_CREDENTIAL, bad

    def test_non_hash_payload_is_malformed(self):
        settings = _settings()
        for payload in ({"type": "hash", "hash": "not-a-tx"}, {"type": "transaction", "signature": "0xabc"}):
            cred = _credential(settings)
            cred["payload"] = payload
            parsed, code, _ = validate_mpp_credential(cred, tool_cost_usdc=_COST, settings=settings)
            assert parsed is None and code == MPP_MALFORMED_CREDENTIAL

    def test_edited_or_self_minted_challenge_fails_binding(self):
        settings = _settings()
        for mutate in (
            lambda c: c["request"].update(amount="1"),
            lambda c: c["request"].update(recipient="0x" + "00" * 20),
            lambda c: c.update(expires="2099-01-01T00:00:00Z"),
            lambda c: c["opaque"].update(iat=0),
        ):
            challenge = build_mpp_challenge(tool_cost_usdc=_COST, settings=settings)
            mutate(challenge)
            parsed, code, msg = validate_mpp_credential(
                _credential(settings, challenge=challenge), tool_cost_usdc=_COST, settings=settings
            )
            assert parsed is None and code == MPP_VERIFY_FAILED and "not issued" in msg

        forged = build_mpp_challenge(tool_cost_usdc=_COST, settings=_settings(mpp_secret_key="x" * 32))
        parsed, code, _ = validate_mpp_credential(
            _credential(settings, challenge=forged), tool_cost_usdc=_COST, settings=settings
        )
        assert parsed is None and code == MPP_VERIFY_FAILED

    def test_price_or_config_drift_after_issue_fails(self):
        settings = _settings()
        cred = _credential(settings)
        parsed, code, _ = validate_mpp_credential(cred, tool_cost_usdc=_COST * 2, settings=settings)
        assert parsed is None and code == MPP_VERIFY_FAILED
        moved = _settings(mpp_recipient="0x" + "ef" * 20)
        parsed, code, _ = validate_mpp_credential(cred, tool_cost_usdc=_COST, settings=moved)
        assert parsed is None and code == MPP_VERIFY_FAILED

    def test_expired_challenge_fails(self):
        settings = _settings()
        stale = build_mpp_challenge(
            tool_cost_usdc=_COST, settings=settings, now=datetime.now(timezone.utc) - timedelta(seconds=600)
        )
        parsed, code, msg = validate_mpp_credential(
            _credential(settings, challenge=stale), tool_cost_usdc=_COST, settings=settings
        )
        assert parsed is None and code == MPP_VERIFY_FAILED and "expired" in msg

    @pytest.mark.parametrize(
        "source",
        ["did:pkh:eip155:1:" + _PAYER, "did:pkh:eip155:8453:nope", "alice", "did:web:example.com"],
    )
    def test_source_must_be_evm_payer_on_this_chain(self, source):
        settings = _settings()
        parsed, code, _ = validate_mpp_credential(_credential(settings, source=source), tool_cost_usdc=_COST, settings=settings)
        assert parsed is None and code == MPP_MALFORMED_CREDENTIAL

    def test_bare_address_source_accepted(self):
        settings = _settings()
        parsed, _, _ = validate_mpp_credential(
            _credential(settings, source=_PAYER.upper().replace("0X", "0x")), tool_cost_usdc=_COST, settings=settings
        )
        assert parsed is not None and parsed.payer == _PAYER


def _receipt(*, value=_COST, sender=_PAYER, to=_RECIPIENT, token=_CURRENCY, status="0x1", block="0x10"):
    def word(address):
        return "0x" + address.lower().removeprefix("0x").rjust(64, "0")

    return {
        "status": status,
        "blockNumber": block,
        "logs": [
            {
                "address": token,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    word(sender),
                    word(to),
                ],
                "data": hex(value),
            }
        ],
    }


def _match(receipt):
    return receipt_matches(receipt, currency=_CURRENCY, recipient=_RECIPIENT, payer=_PAYER, amount_usdc=_COST)


class TestReceiptMatches:
    def test_exact_match_with_rpc_hex_status(self):
        assert _match(_receipt()) is True
        assert _match(_receipt(status=1)) is True

    def test_rejects_any_term_mismatch(self):
        assert _match(_receipt(to="0x" + "00" * 20)) is False
        assert _match(_receipt(token="0x" + "99" * 20)) is False
        assert _match(_receipt(value=_COST - 1)) is False
        assert _match(_receipt(status="0x0")) is False
        assert _match(None) is False
        assert _match({"status": "0x1", "logs": []}) is False

    def test_rejects_transfer_from_someone_else(self):
        """A third party's transfer to the recipient can't be claimed under a different source."""
        assert _match(_receipt(sender="0x" + "77" * 20)) is False


class TestVerifyPayment:
    def _patches(self, *, receipt=None, mined_at=None, claim=True):
        mined_at = int(datetime.now(timezone.utc).timestamp()) if mined_at is None else mined_at
        return (
            patch("billing.mpp._eth_get_receipt", new=AsyncMock(return_value=receipt if receipt is not None else _receipt())),
            patch("billing.mpp._eth_block_timestamp", new=AsyncMock(return_value=mined_at)),
            patch("billing.claim_payment_nonce", new=AsyncMock(return_value=claim)),
            patch("billing.release_payment_nonce", new=AsyncMock()),
        )

    async def test_happy_keeps_claim_and_returns_payer(self):
        settings = _settings()
        p_receipt, p_block, p_claim, p_release = self._patches()
        with p_receipt, p_block, p_claim as claim, p_release as release:
            outcome = await verify_mpp_payment(_credential(settings), tool_cost_usdc=_COST, settings=settings)
        assert outcome.status == "ok"
        assert (outcome.tx_hash, outcome.source) == (_TX, _PAYER)
        assert outcome.challenge_id
        claim.assert_awaited_once_with(mpp_nonce_key(_TX))
        release.assert_not_awaited()

    async def test_replay_is_rejected(self):
        settings = _settings()
        p_receipt, p_block, p_claim, p_release = self._patches(claim=False)
        with p_receipt, p_block, p_claim, p_release:
            outcome = await verify_mpp_payment(_credential(settings), tool_cost_usdc=_COST, settings=settings)
        assert outcome.status == "failed" and "already used" in outcome.message

    async def test_transfer_mined_before_challenge_is_rejected(self):
        """Closes reuse of old transfers whose 24h claim row has been cleaned up."""
        settings = _settings()
        old = int(datetime.now(timezone.utc).timestamp()) - 3600
        p_receipt, p_block, p_claim, p_release = self._patches(mined_at=old)
        with p_receipt, p_block, p_claim, p_release as release:
            outcome = await verify_mpp_payment(_credential(settings), tool_cost_usdc=_COST, settings=settings)
        assert outcome.status == "failed" and "predates" in outcome.message
        release.assert_awaited_once()

    async def test_unconfirmed_or_mismatched_receipt_releases_claim(self):
        settings = _settings()
        with (
            patch("billing.mpp._eth_get_receipt", new=AsyncMock(return_value=None)),
            patch("billing.claim_payment_nonce", new=AsyncMock(return_value=True)),
            patch("billing.release_payment_nonce", new=AsyncMock()) as release,
        ):
            outcome = await verify_mpp_payment(_credential(settings), tool_cost_usdc=_COST, settings=settings)
        assert outcome.status == "failed" and "not yet confirmed" in outcome.message
        release.assert_awaited_once()

        p_receipt, p_block, p_claim, p_release = self._patches(receipt=_receipt(sender="0x" + "77" * 20))
        with p_receipt, p_block, p_claim, p_release as release:
            outcome = await verify_mpp_payment(_credential(settings), tool_cost_usdc=_COST, settings=settings)
        assert outcome.status == "failed"
        release.assert_awaited_once()

    async def test_rpc_outage_is_unavailable_and_releases_claim(self):
        settings = _settings()
        with (
            patch("billing.mpp._eth_get_receipt", new=AsyncMock(side_effect=MppRpcError("down"))),
            patch("billing.claim_payment_nonce", new=AsyncMock(return_value=True)),
            patch("billing.release_payment_nonce", new=AsyncMock()) as release,
        ):
            outcome = await verify_mpp_payment(_credential(settings), tool_cost_usdc=_COST, settings=settings)
        assert outcome.status == "unavailable"
        release.assert_awaited_once()

    async def test_replay_store_outage_fails_closed(self):
        settings = _settings()
        with (
            patch("billing.claim_payment_nonce", new=AsyncMock(side_effect=RuntimeError("db down"))),
            patch("billing.mpp._eth_get_receipt", new=AsyncMock()) as receipt,
        ):
            outcome = await verify_mpp_payment(_credential(settings), tool_cost_usdc=_COST, settings=settings)
        assert outcome.status == "unavailable"
        receipt.assert_not_awaited()

    async def test_invalid_credential_never_touches_store_or_chain(self):
        settings = _settings()
        with (
            patch("billing.claim_payment_nonce", new=AsyncMock()) as claim,
            patch("billing.mpp._eth_get_receipt", new=AsyncMock()) as receipt,
        ):
            outcome = await verify_mpp_payment({"bad": True}, tool_cost_usdc=_COST, settings=settings)
        assert outcome.status == "malformed"
        claim.assert_not_awaited()
        receipt.assert_not_awaited()


class TestHttpCarriers:
    def test_parse_payment_authorization(self):
        assert parse_payment_authorization(None) is None
        assert parse_payment_authorization("******") is None
        assert parse_payment_authorization("Payment " + _b64({"source": "x"})) == {"source": "x"}
        assert parse_payment_authorization("Payment !!!not-base64") == {}

    def test_receipt_header_is_base64url_json(self):
        receipt = {"status": "success", "method": "evm", "reference": _TX}
        raw = encode_receipt(receipt)
        assert "=" not in raw
        assert json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))) == receipt


async def test_strict_claim_raises_when_store_unavailable():
    from billing import claim_payment_nonce

    with patch("billing.x402._has_pool", return_value=False):
        with pytest.raises(RuntimeError):
            await claim_payment_nonce(mpp_nonce_key(_TX))
