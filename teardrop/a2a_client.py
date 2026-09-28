# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""A2A protocol client — agent card discovery and outbound message sending.

Implements the HTTP+JSON/REST binding of the A2A v1.0 specification:
  - GET  /.well-known/agent-card.json   → discover remote agent capabilities
  - POST /message:send                  → send a task to a remote agent
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from teardrop.cache import get_redis

logger = logging.getLogger(__name__)


# ─── SSRF Guard ───────────────────────────────────────────────────────────────

# Single source of truth for the SSRF blocklist lives in tools.definitions.http_fetch.
# Re-exported here so historical callers/tests that import _BLOCKED_NETWORKS and
# _is_ip_blocked from teardrop.a2a_client keep working, while CGNAT/NAT64 and any
# future range additions only need to be maintained in one place.
from tools.definitions.http_fetch import (  # noqa: E402
    _BLOCKED_NETWORKS,  # noqa: F401  (re-exported for backward compatibility)
    _is_ip_blocked,  # noqa: F401  (re-exported for backward compatibility)
)
from tools.definitions.http_fetch import validate_url as _canonical_validate_url  # noqa: E402


def validate_url(url: str) -> str | None:
    """Delegate URL validation to the canonical SSRF implementation."""
    return _canonical_validate_url(url)


async def async_validate_url(url: str) -> str | None:
    """Run the local compatibility wrapper without blocking the event loop."""
    return await asyncio.to_thread(validate_url, url)


def _canonicalize_agent_url(agent_url: str, *, require_https: bool = False) -> str:
    """Return a canonical URL form without performing SSRF validation."""
    value = str(agent_url).strip()
    message = "Agent endpoint must be a valid HTTPS URL." if require_https else "Agent endpoint must be a valid HTTP(S) URL."
    if not value or any(character.isspace() for character in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(message) from None

    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or (require_https and scheme != "https")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(message)

    host = hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return urlunsplit((scheme, f"{host}{port_suffix}", parsed.path.rstrip("/"), "", ""))


# ─── A2A Data Models (subset of v1.0 spec) ───────────────────────────────────


class A2AAgentCard(BaseModel):
    """Remote agent's published capabilities (/.well-known/agent-card.json)."""

    name: str
    description: str = ""
    url: str = ""
    version: str = ""
    price_per_task_usdc: int | None = Field(
        default=None,
        gt=0,
        le=100_000_000,
        strict=True,
        description="Optional advertised task price in atomic USDC.",
    )
    capabilities: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text"])
    authentication: dict[str, Any] | None = None

    model_config = {"extra": "allow"}

    @field_validator("price_per_task_usdc", mode="before")
    @classmethod
    def _drop_unusable_price(cls, value: Any) -> Any:
        # A malformed remote price must not brick the card; fall back to the caller's own cap.
        if value is None or (type(value) is int and 0 < value <= 100_000_000):
            return value
        return None


class A2APart(BaseModel):
    """A single part within an A2A message."""

    kind: str = "text"  # text | data | file
    text: str | None = None
    data: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class A2AMessage(BaseModel):
    """An A2A protocol message."""

    role: str  # "user" | "agent"
    parts: list[A2APart]
    message_id: str | None = Field(default=None, alias="messageId")

    model_config = {"extra": "allow", "populate_by_name": True}


class A2AArtifact(BaseModel):
    """An artifact produced by a remote agent."""

    artifact_id: str | None = Field(default=None, alias="artifactId")
    name: str | None = None
    parts: list[A2APart] = Field(default_factory=list)

    model_config = {"extra": "allow", "populate_by_name": True}


class A2ATaskStatus(BaseModel):
    """Status of an A2A task."""

    state: str  # submitted | working | input-required | completed | failed | canceled
    message: A2AMessage | None = None

    model_config = {"extra": "allow"}

    @field_validator("state", mode="before")
    @classmethod
    def _normalize_v1_state(cls, value: Any) -> Any:
        # A2A 1.0 uses TASK_STATE_INPUT_REQUIRED where 0.3 uses input-required.
        if isinstance(value, str) and value.startswith("TASK_STATE_"):
            return value.removeprefix("TASK_STATE_").lower().replace("_", "-")
        return value


class A2ATask(BaseModel):
    """Top-level A2A task object returned by /message:send."""

    id: str
    status: A2ATaskStatus
    artifacts: list[A2AArtifact] = Field(default_factory=list)
    history: list[A2AMessage] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class A2ASendMessageResponse(BaseModel):
    """Parsed response from POST /message:send.

    The remote agent may return either a Task object directly or wrap it in
    a JSON-RPC-style envelope with ``result``.  We normalise both shapes.
    """

    task: A2ATask | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    settlement_tx: str = ""
    payment_amount_usdc: int = 0

    model_config = {"extra": "allow"}


# ─── Agent-card cache ─────────────────────────────────────────────────────────

_agent_card_cache: dict[str, tuple[A2AAgentCard, float]] = {}
_AGENT_CARD_CACHE_MAX_ENTRIES = 10_000
_AGENT_CARD_REDIS_PREFIX = "teardrop:a2a:agent-card:"


def _redis_cache_key(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{_AGENT_CARD_REDIS_PREFIX}{digest}"


def _cache_get(url: str, ttl: int) -> A2AAgentCard | None:
    entry = _agent_card_cache.get(url)
    if entry is None:
        return None
    card, ts = entry
    if time.monotonic() - ts > ttl:
        _agent_card_cache.pop(url, None)
        return None
    return card


def _cache_set(url: str, card: A2AAgentCard) -> None:
    if url not in _agent_card_cache and len(_agent_card_cache) >= _AGENT_CARD_CACHE_MAX_ENTRIES:
        oldest_url = min(_agent_card_cache, key=lambda cached_url: _agent_card_cache[cached_url][1])
        _agent_card_cache.pop(oldest_url, None)
    _agent_card_cache[url] = (card, time.monotonic())


async def _redis_cache_get(url: str) -> A2AAgentCard | None:
    redis = get_redis()
    if redis is None:
        return None
    try:
        raw = await redis.get(_redis_cache_key(url))
        if raw is None:
            return None
        return A2AAgentCard.model_validate_json(raw)
    except Exception as exc:
        logger.warning("Redis agent-card cache read failed; falling back: %s", exc)
        return None


async def _redis_cache_set(url: str, card: A2AAgentCard, ttl: int) -> None:
    if ttl <= 0:
        return
    redis = get_redis()
    if redis is None:
        return
    try:
        await redis.setex(_redis_cache_key(url), ttl, card.model_dump_json())
    except Exception as exc:
        logger.warning("Redis agent-card cache write failed (non-fatal): %s", exc)


# ─── Public API ───────────────────────────────────────────────────────────────

_USER_AGENT = "Teardrop/1.0 (A2A Client; +https://teardrop.ai)"


async def discover_agent_card(
    base_url: str,
    *,
    timeout: int = 10,
    cache_ttl: int = 300,
    bypass_cache: bool = False,
) -> A2AAgentCard:
    """Fetch and parse a remote agent's A2A agent card.

    Args:
        base_url: The base URL of the remote agent (e.g. ``https://agent.example.com``).
        timeout: HTTP request timeout in seconds.
        cache_ttl: How long to cache the card in seconds.
        bypass_cache: Skip cached copies and fetch a fresh card (the fresh card is still cached).

    Raises:
        ValueError: If the URL fails SSRF validation.
        httpx.HTTPStatusError: If the remote server returns a non-2xx status.
        Exception: If the response body is not valid JSON or fails Pydantic validation.
    """
    # Normalise: strip trailing slash
    base_url = base_url.rstrip("/")

    # Check cache first
    cached = None if bypass_cache else _cache_get(base_url, cache_ttl)
    if cached is not None:
        logger.debug("discover_agent_card: cache hit for %s", base_url)
        return cached

    cached = None if bypass_cache else await _redis_cache_get(base_url)
    if cached is not None:
        _cache_set(base_url, cached)
        logger.debug("discover_agent_card: Redis cache hit for %s", base_url)
        return cached

    # SSRF check
    ssrf_err = await async_validate_url(base_url)
    if ssrf_err:
        raise ValueError(f"SSRF blocked: {ssrf_err}")

    card_url = f"{base_url}/.well-known/agent-card.json"
    logger.info("discover_agent_card: fetching %s", card_url)

    from tools.definitions.http_fetch import make_ssrf_safe_httpx_transport

    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=False,
        transport=make_ssrf_safe_httpx_transport(),
    ) as client:
        resp = await client.get(card_url)
        resp.raise_for_status()

    card = A2AAgentCard.model_validate(resp.json())
    _cache_set(base_url, card)
    await _redis_cache_set(base_url, card, cache_ttl)
    return card


async def send_message(
    base_url: str,
    message_text: str,
    *,
    timeout: int = 120,
    auth_header: str | None = None,
) -> A2ASendMessageResponse:
    """Send a task message to a remote A2A agent via POST /message:send.

    Uses the HTTP+JSON/REST binding (A2A v1.0, Section 11).

    Args:
        base_url: The base URL of the remote agent.
        message_text: The user-role message text to send.
        timeout: HTTP request timeout in seconds.
        auth_header: Optional Bearer token to attach as Authorization header.

    Raises:
        ValueError: If the URL fails SSRF validation.
        httpx.HTTPStatusError: On non-2xx response.
    """
    base_url = base_url.rstrip("/")

    ssrf_err = await async_validate_url(base_url)
    if ssrf_err:
        raise ValueError(f"SSRF blocked: {ssrf_err}")

    endpoint = f"{base_url}/message:send"
    payload: dict[str, Any] = {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": message_text}],
        },
    }

    headers = {
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if auth_header:
        headers["Authorization"] = f"Bearer {auth_header}"

    logger.info("send_message: POST %s (auth=%s)", endpoint, bool(auth_header))

    from tools.definitions.http_fetch import make_ssrf_safe_httpx_transport

    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=False,
            transport=make_ssrf_safe_httpx_transport(),
        ) as client:
            resp = await client.post(endpoint, json=payload)
            resp.raise_for_status()

        data = resp.json()
    return _parse_send_response(data)


def _parse_send_response(data: dict[str, Any], *, settlement_tx: str = "") -> A2ASendMessageResponse:
    """Normalise a /message:send response: raw Task, JSON-RPC envelope, or A2A 1.0 ``{"task": ...}``.

    Anything else yields ``task=None``, which callers must treat as a failure.
    """
    if not isinstance(data, dict):
        logger.warning("send_message: response body is not a JSON object")
        return A2ASendMessageResponse(settlement_tx=settlement_tx)

    task_data = data.get("result", data)
    if isinstance(task_data, dict) and isinstance(task_data.get("task"), dict):
        task_data = task_data["task"]

    try:
        task = A2ATask.model_validate(task_data)
    except Exception:
        logger.warning("send_message: could not parse task from response")
        return A2ASendMessageResponse(raw=data, settlement_tx=settlement_tx)

    return A2ASendMessageResponse(task=task, raw=data, settlement_tx=settlement_tx)


def _requirement_value(requirement: Any, key: str, default: Any = None) -> Any:
    if isinstance(requirement, dict):
        return requirement.get(key, default)
    return getattr(requirement, key, default)


def extract_result_text(response: A2ASendMessageResponse) -> str:
    """Extract human-readable text from an A2A send-message response."""
    if response.task is None:
        return str(response.raw) if response.raw else "No response from remote agent."

    task = response.task

    # 1. Try artifacts
    for artifact in task.artifacts:
        for part in artifact.parts:
            if part.text:
                return part.text

    # 2. Try status message
    if task.status.message:
        for part in task.status.message.parts:
            if part.text:
                return part.text

    # 3. Try last message in history
    if task.history:
        last = task.history[-1]
        for part in last.parts:
            if part.text:
                return part.text

    return f"Remote agent completed with state: {task.status.state}"


# ─── Allowlist enforcement ────────────────────────────────────────────────────


async def check_delegation_allowed(org_id: str, agent_url: str, pool) -> tuple[bool, dict | None]:
    """Check if *agent_url* is in the org's a2a_allowed_agents table.

    Returns (allowed, row_dict) — row_dict contains max_cost_usdc, require_x402,
    jwt_forward, source, and listing_active when the agent is found, or None when
    not found. A self-serve row whose agent is no longer registered by another
    org is returned with ``allowed=False``.
    """
    candidates = [agent_url.rstrip("/")]
    try:
        canonical = _canonicalize_agent_url(agent_url)
    except ValueError:
        canonical = ""
    if canonical and canonical not in candidates:
        candidates.append(canonical)
    row = await pool.fetchrow(
        """
        SELECT a.id, a.agent_url, a.label, a.max_cost_usdc, a.require_x402, a.jwt_forward, a.source, a.created_at,
               (a.source <> 'self_serve' OR EXISTS (
                    SELECT 1 FROM a2a_agent_registry AS r
                    WHERE r.agent_url = a.agent_url AND r.org_id <> a.org_id
               )) AS listing_active
        FROM a2a_allowed_agents AS a
        WHERE a.org_id = $1 AND a.agent_url = ANY($2::text[])
        ORDER BY a.created_at
        LIMIT 1
        """,
        org_id,
        candidates,
    )
    if row is None:
        return False, None
    rule = dict(row)
    return rule.get("listing_active", True) is not False, rule


# ─── Registration probe (never signs) ──────────────────────────────────────────────


async def probe_message_endpoint(base_url: str, message_text: str, *, timeout: float) -> httpx.Response:
    """POST Teardrop's legacy message body without payment or auth and return the raw response."""
    base_url = base_url.rstrip("/")
    ssrf_err = await async_validate_url(base_url)
    if ssrf_err:
        raise ValueError(f"SSRF blocked: {ssrf_err}")

    from tools.definitions.http_fetch import make_ssrf_safe_httpx_transport

    payload = {"message": {"role": "user", "parts": [{"kind": "text", "text": message_text}]}}
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"},
            follow_redirects=False,
            transport=make_ssrf_safe_httpx_transport(),
        ) as client:
            return await client.post(f"{base_url}/message:send", json=payload)


def exact_payment_offer_amounts(resp: httpx.Response, network: str) -> list[int]:
    """Return positive ``exact`` offer amounts on *network* from a 402 response (empty if undecodable)."""
    try:
        payment_required = _decode_payment_required(resp)
    except Exception:
        return []
    return sorted(
        amount
        for requirement in payment_required.accepts
        if _requirement_value(requirement, "scheme") == "exact"
        and _requirement_value(requirement, "network") == network
        and (amount := _payment_requirement_amount(requirement)) is not None
    )


# ─── x402-aware outbound delegation ──────────────────────────────────────────


async def send_message_with_payment(
    base_url: str,
    message_text: str,
    *,
    signer=None,
    timeout: int = 120,
    auth_header: str | None = None,
    max_amount_atomic: int,
    allowed_networks: frozenset[str],
    payment_attempt_callback: Callable[[int], Awaitable[None]] | None = None,
) -> A2ASendMessageResponse:
    """Send a task message to a remote A2A agent, handling x402 payment if required.

    If the remote agent returns HTTP 402, this function selects one ``exact`` offer
    within *max_amount_atomic*, signs it using *signer*, awaits
    *payment_attempt_callback* with the signed amount, and retries once with the
    ``X-PAYMENT`` header attached.

    Falls back to ``send_message()`` behaviour when *signer* is None or the
    remote agent does not require payment.
    """
    base_url = base_url.rstrip("/")

    ssrf_err = await async_validate_url(base_url)
    if ssrf_err:
        raise ValueError(f"SSRF blocked: {ssrf_err}")

    endpoint = f"{base_url}/message:send"
    payload: dict[str, Any] = {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": message_text}],
        },
    }

    headers = {
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if auth_header:
        headers["Authorization"] = f"Bearer {auth_header}"

    from tools.definitions.http_fetch import make_ssrf_safe_httpx_transport

    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=False,
            transport=make_ssrf_safe_httpx_transport(),
        ) as client:
            resp = await client.post(endpoint, json=payload)
            payment_amount_usdc = 0

            # ── Handle 402 Payment Required ───────────────────────────────
            if resp.status_code == 402 and signer is not None:
                signed = _sign_x402_payment(
                    resp,
                    signer,
                    max_amount_atomic=max_amount_atomic,
                    allowed_networks=allowed_networks,
                )
                if signed:
                    payment_header, payment_amount_usdc = signed
                    if payment_attempt_callback is not None:
                        await payment_attempt_callback(payment_amount_usdc)
                    resp = await client.post(
                        endpoint,
                        json=payload,
                        headers={"X-PAYMENT": payment_header},
                    )

            resp.raise_for_status()
            settlement_tx = _extract_payment_response_transaction(resp)

        data = resp.json()
    response = _parse_send_response(data, settlement_tx=settlement_tx)
    response.payment_amount_usdc = payment_amount_usdc
    return response


_PAYMENT_TRANSACTION_PATTERN = re.compile(r"^0x[a-fA-F0-9]{64}$")


def _extract_payment_response_transaction(resp: httpx.Response) -> str:
    """Return a bounded x402 transaction hash without exposing header contents."""
    for header_name in ("PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"):
        header_value = resp.headers.get(header_name)
        if not isinstance(header_value, str) or not header_value:
            continue
        try:
            from x402.http import decode_payment_response_header

            settlement = decode_payment_response_header(header_value)
            transaction = getattr(settlement, "transaction", "")
            if isinstance(transaction, str) and _PAYMENT_TRANSACTION_PATTERN.fullmatch(transaction):
                return transaction
        except Exception:
            logger.debug("send_message_with_payment: invalid payment response metadata", exc_info=True)
    return ""


def _payment_requirement_amount(requirement: Any) -> int | None:
    """Read an x402 atomic amount across current and legacy field names."""
    raw_amount = _requirement_value(requirement, "amount")
    if raw_amount is None and isinstance(requirement, dict):
        raw_amount = requirement.get("maxAmountRequired")
    if raw_amount is None:
        raw_amount = _requirement_value(requirement, "max_amount_required")
    try:
        amount = int(raw_amount)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _decode_payment_required(resp: httpx.Response) -> Any:
    """Decode x402 requirements from the v2 header, then legacy header and body fallbacks."""
    import base64
    import json as _json

    from x402.schemas.payments import PaymentRequired

    standard_header = resp.headers.get("PAYMENT-REQUIRED", "")
    if standard_header:
        from x402.http import decode_payment_required_header

        return decode_payment_required_header(standard_header)
    legacy_header = resp.headers.get("X-PAYMENT-REQUIRED", "")
    if legacy_header:
        return PaymentRequired.model_validate(
            {
                "x402Version": 2,
                "accepts": _json.loads(base64.b64decode(legacy_header)),
            }
        )
    return PaymentRequired.model_validate(resp.json())


def _sign_x402_payment(
    resp: httpx.Response,
    signer,
    *,
    max_amount_atomic: int,
    allowed_networks: frozenset[str],
) -> tuple[str, int] | None:
    """Sign the cheapest ``exact`` offer within *max_amount_atomic* on an allowed network.

    Returns ``(header, signed_amount_atomic)``, or None if no offer qualifies or signing fails.
    """
    import base64
    import json as _json

    try:
        from x402 import x402ClientSync
        from x402.mechanisms.evm.exact import ExactEvmScheme

        payment_required = _decode_payment_required(resp)

        # upto lets the seller settle any amount up to the signed max, so only exact is accepted.
        accepted_requirements = [
            (amount, requirement)
            for requirement in payment_required.accepts
            if _requirement_value(requirement, "scheme") == "exact"
            and _requirement_value(requirement, "network") in allowed_networks
            and (amount := _payment_requirement_amount(requirement)) is not None
            and amount <= max_amount_atomic
        ]
        if not accepted_requirements:
            logger.warning("_sign_x402_payment: no exact payment requirement satisfied the delegation cap")
            return None
        signed_amount, selected = min(accepted_requirements, key=lambda item: item[0])
        if len(payment_required.accepts) != 1:
            payment_required = payment_required.model_copy(update={"accepts": [selected]})

        client = x402ClientSync()
        client.register(str(_requirement_value(selected, "network")), ExactEvmScheme(signer=signer))

        payload = client.create_payment_payload(payment_required)

        # Encode payload for the X-PAYMENT header.
        payload_json = _json.dumps(
            payload.model_dump(by_alias=True, exclude_none=True) if hasattr(payload, "model_dump") else payload,
            default=str,
        )
        return base64.b64encode(payload_json.encode()).decode(), signed_amount
    except Exception:
        logger.exception("_sign_x402_payment: failed to sign payment")
        return None
