# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""MPP ``evm`` charge verification for the MCP gateway (client-broadcast ``hash`` credentials).

Specs (accessed 2026-10-02): paymentauth.org/draft-evm-charge-00 §4.2, §6.4, §8, §10.4 and
mpp.dev/protocol/{challenges,credentials,receipts,transports/mcp}. Only ``type="hash"`` is
accepted, so every Challenge advertises ``methodDetails.credentialTypes=["hash"]``.

Hash credentials are weakly bound (§10.4): nothing on-chain ties a transfer to a Challenge.
Defences layered here:

* Challenge ids are HMAC-bound over ``realm|method|intent|request|expires|digest|opaque``; the
  opaque carries the issue time, so edited or self-minted Challenges are rejected.
* The ERC-20 Transfer must come from the credential ``source`` and be mined no earlier than the
  Challenge issue time, so pre-existing transfers — including any whose 24h replay-claim row has
  been cleaned up — can never pay.
* ``mpp_recipient`` may not be an x402 treasury address (config validator), so x402 and top-up
  settlements can't be re-presented as MPP payments.
* The ``mpp:{tx_hash}`` claim fails closed and is never released once a payment is accepted.

Settlement is implicit: the transfer is final before execution.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# MCP-transport JSON-RPC payment error codes (MPP spec table).
MPP_PAYMENT_REQUIRED = -32042
MPP_VERIFY_FAILED = -32043
MPP_MALFORMED_CREDENTIAL = -32602

MPP_CREDENTIAL_META_KEY = "org.paymentauth/credential"
MPP_RECEIPT_META_KEY = "org.paymentauth/receipt"
MPP_CHALLENGES_META_KEY = "org.paymentauth/challenges"
MPP_METHOD = "evm"
MPP_INTENT = "charge"

# keccak256("Transfer(address,address,uint256)") — the ERC-20 event topic.
_ERC20_TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# methodDetails.permit2Address is REQUIRED by the draft even though only "hash" is accepted.
_PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
_USDC_DECIMALS = 6
_RPC_TIMEOUT_SECONDS = 10.0
# Block timestamps vs. server clock; anything older than issue time minus this is a pre-existing transfer.
_ISSUE_SKEW_SECONDS = 60
_MIN_SECRET_LENGTH = 32
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")


class MppRpcError(RuntimeError):
    """RPC transport/protocol failure — callers fail closed (503)."""


def mpp_chain_id(settings: Any) -> int | None:
    """EIP-155 chain id from ``x402_network`` (``eip155:<id>``); MPP and x402 share Base."""
    prefix, _, reference = str(getattr(settings, "x402_network", "") or "").partition(":")
    return int(reference) if prefix == "eip155" and reference.isdigit() else None


def mpp_configured(settings: Any) -> bool:
    """True only when an MPP payment can be verified, gated, billed and recorded.

    MPP rides the anonymous paid path, so it also needs MCP auth, x402 fallback and MCP billing
    enabled; otherwise a payer could transfer funds and then be refused by an inactive gate.
    Anything less leaves the gateway on x402 alone.
    """
    return (
        bool(getattr(settings, "mpp_enabled", False))
        and bool(getattr(settings, "mcp_auth_enabled", False))
        and bool(getattr(settings, "mcp_x402_enabled", False))
        and bool(getattr(settings, "mcp_billing_enabled", False))
        and bool(getattr(settings, "mpp_recipient", ""))
        and bool(getattr(settings, "mpp_currency", ""))
        and bool(getattr(settings, "base_rpc_url", ""))
        and len(str(getattr(settings, "mpp_secret_key", "") or "")) >= _MIN_SECRET_LENGTH
        and mpp_chain_id(settings) is not None
    )


def mpp_nonce_key(tx_hash: str) -> str:
    """Replay key; non-x402 keys hash through ``_payment_nonce_hash`` as raw strings."""
    return f"mpp:{str(tx_hash).strip().lower()}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _jcs(value: Any) -> str:
    # Sorted-key compact JSON equals RFC 8785 JCS for the string/int/list values used here.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _encode_field(value: dict) -> str:
    return _b64url(_jcs(value).encode("utf-8"))


def _decode_field(value: Any) -> dict | None:
    """Accept the native MCP object form or the HTTP base64url-JCS string form."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(_b64url_decode(value))
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _challenge_id(*, secret: str, realm: str, method: str, intent: str, request: dict, expires: str, opaque: dict) -> str:
    # Spec order with no digest and no header: realm|method|intent|request|expires|digest|opaque.
    message = "|".join([realm, method, intent, _encode_field(request), expires, "", _encode_field(opaque)])
    return _b64url(hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest())


def build_mpp_challenge(*, tool_cost_usdc: int, settings: Any, now: datetime | None = None) -> dict[str, Any]:
    """Mint an HMAC-bound evm charge Challenge for the resolved atomic-USDC tool price."""
    now = now or datetime.now(timezone.utc)
    request = {
        "amount": str(int(tool_cost_usdc)),
        "currency": str(settings.mpp_currency).strip(),
        "recipient": str(settings.mpp_recipient).strip(),
        "methodDetails": {
            "chainId": mpp_chain_id(settings),
            "credentialTypes": ["hash"],
            "decimals": _USDC_DECIMALS,
            "permit2Address": _PERMIT2_ADDRESS,
        },
    }
    opaque = {"iat": int(now.timestamp()), "nonce": secrets.token_urlsafe(9)}
    expires = _iso(now + timedelta(seconds=int(settings.mpp_challenge_ttl_seconds)))
    challenge: dict[str, Any] = {
        "realm": settings.app_host,
        "method": MPP_METHOD,
        "intent": MPP_INTENT,
        "request": request,
        "expires": expires,
        "opaque": opaque,
    }
    challenge["id"] = _challenge_id(
        secret=str(settings.mpp_secret_key),
        realm=challenge["realm"],
        method=MPP_METHOD,
        intent=MPP_INTENT,
        request=request,
        expires=expires,
        opaque=opaque,
    )
    return challenge


def mpp_www_authenticate(challenge: dict[str, Any]) -> str:
    """HTTP-transport form of a Challenge (``WWW-Authenticate: Payment ...``)."""
    params = (
        ("id", challenge["id"]),
        ("realm", challenge["realm"]),
        ("method", challenge["method"]),
        ("intent", challenge["intent"]),
        ("request", _encode_field(challenge["request"])),
        ("expires", challenge["expires"]),
        ("opaque", _encode_field(challenge["opaque"])),
    )
    return "Payment " + ", ".join(f'{key}="{value}"' for key, value in params)


def parse_payment_authorization(header_value: str | None) -> Any:
    """Credential from ``Authorization: Payment <base64url JSON>``; None when another scheme is used.

    An undecodable token returns ``{}`` so validation answers ``-32602`` instead of silently
    falling back to an x402 challenge.
    """
    scheme, _, token = str(header_value or "").strip().partition(" ")
    if scheme.lower() != "payment":
        return None
    try:
        return json.loads(_b64url_decode(token.strip()))
    except (ValueError, TypeError):
        return {}


def encode_receipt(receipt: dict[str, Any]) -> str:
    """``Payment-Receipt`` header value (base64url JSON)."""
    return _encode_field(receipt)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _source_address(source: str, chain_id: int) -> str | None:
    """Lower-case payer address from ``0x…`` or ``did:pkh:eip155:<chainId>:0x…``."""
    value = source.strip()
    if value.lower().startswith("did:pkh:eip155:"):
        parts = value.split(":")
        if len(parts) != 5 or parts[3] != str(chain_id):
            return None
        value = parts[4]
    return value.lower() if _ADDRESS_RE.fullmatch(value) else None


@dataclass(frozen=True)
class ParsedMppCredential:
    tx_hash: str
    payer: str
    challenge_id: str
    issued_at: int


def validate_mpp_credential(
    credential: Any, *, tool_cost_usdc: int, settings: Any, now: datetime | None = None
) -> tuple[ParsedMppCredential | None, int | None, str | None]:
    """Structure, Challenge binding, payment terms, expiry and payer identity — no I/O.

    Returns ``(parsed, None, None)`` or ``(None, code, message)`` with ``-32602`` for a malformed
    credential and ``-32043`` for binding/terms/expiry failures (retryable with a new Challenge).
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(credential, dict):
        return None, MPP_MALFORMED_CREDENTIAL, "Credential must be an object."
    challenge = credential.get("challenge")
    payload = credential.get("payload")
    source = credential.get("source")
    if not isinstance(challenge, dict) or not isinstance(payload, dict) or not isinstance(source, str) or not source.strip():
        return None, MPP_MALFORMED_CREDENTIAL, "Credential missing challenge, payload, or source."
    tx_hash = payload.get("hash")
    if payload.get("type") != "hash" or not isinstance(tx_hash, str) or not _TX_HASH_RE.fullmatch(tx_hash):
        return None, MPP_MALFORMED_CREDENTIAL, 'Only type="hash" credentials with a 0x transaction hash are accepted.'

    request = _decode_field(challenge.get("request"))
    opaque = _decode_field(challenge.get("opaque"))
    fields = [challenge.get(key) for key in ("id", "realm", "method", "intent", "expires")]
    if request is None or opaque is None or not all(isinstance(field, str) for field in fields):
        return None, MPP_MALFORMED_CREDENTIAL, "Challenge is missing required fields."
    challenge_id, realm, method, intent, expires_raw = fields

    expected_id = _challenge_id(
        secret=str(settings.mpp_secret_key),
        realm=realm,
        method=method,
        intent=intent,
        request=request,
        expires=expires_raw,
        opaque=opaque,
    )
    if not hmac.compare_digest(challenge_id, expected_id):
        return None, MPP_VERIFY_FAILED, "Challenge was not issued by this server."
    if method != MPP_METHOD or intent != MPP_INTENT or realm != settings.app_host:
        return None, MPP_VERIFY_FAILED, "Unsupported challenge realm, method, or intent."

    chain_id = mpp_chain_id(settings)
    details = request.get("methodDetails")
    if str(request.get("amount", "")) != str(int(tool_cost_usdc)):
        return None, MPP_VERIFY_FAILED, "Challenge amount does not match the tool price."
    if str(request.get("currency", "")).lower() != str(settings.mpp_currency).strip().lower():
        return None, MPP_VERIFY_FAILED, "Challenge currency does not match."
    if str(request.get("recipient", "")).lower() != str(settings.mpp_recipient).strip().lower():
        return None, MPP_VERIFY_FAILED, "Challenge recipient does not match."
    if not isinstance(details, dict) or details.get("chainId") != chain_id:
        return None, MPP_VERIFY_FAILED, "Challenge chain does not match."

    expires = _parse_iso(expires_raw)
    issued_at = opaque.get("iat")
    if expires is None or type(issued_at) is not int:
        return None, MPP_VERIFY_FAILED, "Challenge expiry or issue time is invalid."
    if expires <= now:
        return None, MPP_VERIFY_FAILED, "Challenge expired."
    if expires > now + timedelta(seconds=int(settings.mpp_challenge_ttl_seconds) + 60):
        return None, MPP_VERIFY_FAILED, "Challenge expiry is too far in the future."

    payer = _source_address(source, chain_id)
    if payer is None:
        return None, MPP_MALFORMED_CREDENTIAL, f"source must be a 0x address or did:pkh:eip155:{chain_id}:<address>."
    return ParsedMppCredential(tx_hash.lower(), payer, challenge_id, issued_at), None, None


def _topic_address(address: str) -> str:
    return "0x" + str(address).lower().removeprefix("0x").rjust(64, "0")


def receipt_matches(receipt: Any, *, currency: str, recipient: str, payer: str, amount_usdc: int) -> bool:
    """True when a successful receipt has an exact-value ERC-20 Transfer payer → recipient (§6.4).

    Addresses compare by value: token log address == currency, topic1 == payer, topic2 ==
    recipient, and data == the atomic-USDC amount (charge is a fixed-amount intent).
    """
    if not isinstance(receipt, dict) or str(receipt.get("status", "")).lower() not in ("0x1", "1"):
        return False
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return False
    from_word, to_word = _topic_address(payer), _topic_address(recipient)
    for log in logs:
        if not isinstance(log, dict):
            continue
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) < 3:
            continue
        if str(log.get("address", "")).lower() != str(currency).lower():
            continue
        if str(topics[0]).lower() != _ERC20_TRANSFER_TOPIC0:
            continue
        if str(topics[1]).lower() != from_word or str(topics[2]).lower() != to_word:
            continue
        try:
            value = int(str(log.get("data", "0x0")), 16)
        except ValueError:
            continue
        if value == amount_usdc:
            return True
    return False


async def _eth_call(method: str, params: list, settings: Any) -> Any:
    """JSON-RPC call to the operator-configured ``base_rpc_url``; raises ``MppRpcError`` on failure."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with httpx.AsyncClient(timeout=_RPC_TIMEOUT_SECONDS) as client:
            response = await client.post(str(settings.base_rpc_url), json=payload)
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001 — any transport failure must fail closed
        raise MppRpcError(f"{method} failed: {type(exc).__name__}") from exc
    if not isinstance(body, dict) or body.get("error") is not None:
        raise MppRpcError(f"{method} returned an RPC error")
    return body.get("result")


async def _eth_get_receipt(tx_hash: str, settings: Any) -> dict | None:
    result = await _eth_call("eth_getTransactionReceipt", [tx_hash], settings)
    return result if isinstance(result, dict) else None


async def _eth_block_timestamp(block_number: Any, settings: Any) -> int:
    if not isinstance(block_number, str) or not block_number.startswith("0x"):
        raise MppRpcError("receipt has no blockNumber")
    block = await _eth_call("eth_getBlockByNumber", [block_number, False], settings)
    try:
        return int(str(block["timestamp"]), 16)
    except (KeyError, TypeError, ValueError) as exc:
        raise MppRpcError("block has no timestamp") from exc


@dataclass
class MppVerifyOutcome:
    """status: ok | malformed | failed | unavailable. ``source`` is the verified payer address."""

    status: str
    message: str = ""
    tx_hash: str = ""
    source: str = ""
    challenge_id: str = ""


async def _release(nonce_key: str) -> None:
    from billing import release_payment_nonce

    try:
        await release_payment_nonce(nonce_key)
    except Exception:
        logger.warning("MPP replay-claim release failed", exc_info=True)


async def verify_mpp_payment(credential: Any, *, tool_cost_usdc: int, settings: Any) -> MppVerifyOutcome:
    """Validate → claim tx hash (fail closed) → receipt → payer/terms → mined after issue.

    The claim is kept only on success; every rejection releases it so the legitimate payer can
    retry with the same credential before the Challenge expires.
    """
    parsed, code, message = validate_mpp_credential(credential, tool_cost_usdc=tool_cost_usdc, settings=settings)
    if parsed is None:
        status = "malformed" if code == MPP_MALFORMED_CREDENTIAL else "failed"
        return MppVerifyOutcome(status, message or "Credential rejected.")

    from billing import claim_payment_nonce

    nonce_key = mpp_nonce_key(parsed.tx_hash)
    try:
        claimed = await claim_payment_nonce(nonce_key)
    except Exception:
        logger.warning("MPP replay store unavailable; failing closed", exc_info=True)
        return MppVerifyOutcome("unavailable", "Payment verification is temporarily unavailable.")
    if not claimed:
        return MppVerifyOutcome("failed", "Credential already used.")

    try:
        receipt = await _eth_get_receipt(parsed.tx_hash, settings)
        if receipt is None:
            await _release(nonce_key)
            return MppVerifyOutcome("failed", "Transaction not found or not yet confirmed; retry before the challenge expires.")
        if not receipt_matches(
            receipt,
            currency=str(settings.mpp_currency),
            recipient=str(settings.mpp_recipient),
            payer=parsed.payer,
            amount_usdc=int(tool_cost_usdc),
        ):
            await _release(nonce_key)
            return MppVerifyOutcome("failed", "On-chain transfer does not match the challenge or source.")
        mined_at = await _eth_block_timestamp(receipt.get("blockNumber"), settings)
    except MppRpcError as exc:
        await _release(nonce_key)
        logger.warning("MPP receipt verification unavailable: %s", exc)
        return MppVerifyOutcome("unavailable", "Payment verification is temporarily unavailable.")

    if mined_at < parsed.issued_at - _ISSUE_SKEW_SECONDS:
        await _release(nonce_key)
        return MppVerifyOutcome("failed", "Transfer predates the challenge; pay after receiving a challenge.")

    return MppVerifyOutcome("ok", tx_hash=parsed.tx_hash, source=parsed.payer, challenge_id=parsed.challenge_id)
