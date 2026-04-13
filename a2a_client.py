# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""A2A protocol client — agent card discovery and outbound message sending.

Implements the HTTP+JSON/REST binding of the A2A v1.0 specification:
  - GET  /.well-known/agent-card.json   → discover remote agent capabilities
  - POST /message:send                  → send a task to a remote agent
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ─── SSRF Guard ───────────────────────────────────────────────────────────────

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_ip_blocked(ip_str: str) -> bool:
    """Check if an IP address falls within any blocked range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return any(addr in net for net in _BLOCKED_NETWORKS)


def validate_url(url: str) -> str | None:
    """Validate a URL for SSRF safety. Returns error message or None if safe."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"

    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme} (only http/https allowed)"

    hostname = parsed.hostname
    if not hostname:
        return "No hostname in URL"

    # Block raw IP addresses in private ranges
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_ip_blocked(str(addr)):
            return f"Blocked IP address: {hostname}"
    except ValueError:
        pass  # Not a raw IP — resolve via DNS below

    # DNS resolution check
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _family, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            if _is_ip_blocked(ip_str):
                return f"Hostname {hostname} resolves to blocked IP: {ip_str}"
    except socket.gaierror:
        return f"DNS resolution failed for: {hostname}"

    return None


# ─── A2A Data Models (subset of v1.0 spec) ───────────────────────────────────


class A2AAgentCard(BaseModel):
    """Remote agent's published capabilities (/.well-known/agent-card.json)."""

    name: str
    description: str = ""
    url: str = ""
    version: str = ""
    capabilities: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text"])
    authentication: dict[str, Any] | None = None

    model_config = {"extra": "allow"}


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

    model_config = {"extra": "allow"}


# ─── In-process agent-card cache ─────────────────────────────────────────────

_agent_card_cache: dict[str, tuple[A2AAgentCard, float]] = {}


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
    _agent_card_cache[url] = (card, time.monotonic())


# ─── Public API ───────────────────────────────────────────────────────────────

_USER_AGENT = "Teardrop/1.0 (A2A Client; +https://teardrop.ai)"


async def discover_agent_card(
    base_url: str,
    *,
    timeout: int = 10,
    cache_ttl: int = 300,
) -> A2AAgentCard:
    """Fetch and parse a remote agent's A2A agent card.

    Args:
        base_url: The base URL of the remote agent (e.g. ``https://agent.example.com``).
        timeout: HTTP request timeout in seconds.
        cache_ttl: How long to cache the card in seconds.

    Raises:
        ValueError: If the URL fails SSRF validation.
        httpx.HTTPStatusError: If the remote server returns a non-2xx status.
        Exception: If the response body is not valid JSON or fails Pydantic validation.
    """
    # Normalise: strip trailing slash
    base_url = base_url.rstrip("/")

    # Check cache first
    cached = _cache_get(base_url, cache_ttl)
    if cached is not None:
        logger.debug("discover_agent_card: cache hit for %s", base_url)
        return cached

    # SSRF check
    ssrf_err = validate_url(base_url)
    if ssrf_err:
        raise ValueError(f"SSRF blocked: {ssrf_err}")

    card_url = f"{base_url}/.well-known/agent-card.json"
    logger.info("discover_agent_card: fetching %s", card_url)

    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    ) as client:
        resp = await client.get(card_url)
        resp.raise_for_status()

    card = A2AAgentCard.model_validate(resp.json())
    _cache_set(base_url, card)
    return card


async def send_message(
    base_url: str,
    message_text: str,
    *,
    timeout: int = 120,
) -> A2ASendMessageResponse:
    """Send a task message to a remote A2A agent via POST /message:send.

    Uses the HTTP+JSON/REST binding (A2A v1.0, Section 11).

    Args:
        base_url: The base URL of the remote agent.
        message_text: The user-role message text to send.
        timeout: HTTP request timeout in seconds.

    Raises:
        ValueError: If the URL fails SSRF validation.
        httpx.HTTPStatusError: On non-2xx response.
    """
    base_url = base_url.rstrip("/")

    ssrf_err = validate_url(base_url)
    if ssrf_err:
        raise ValueError(f"SSRF blocked: {ssrf_err}")

    endpoint = f"{base_url}/message:send"
    payload: dict[str, Any] = {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": message_text}],
        },
    }

    logger.info("send_message: POST %s", endpoint)

    async with httpx.AsyncClient(
        timeout=timeout,
        headers={
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        follow_redirects=False,
    ) as client:
        resp = await client.post(endpoint, json=payload)
        resp.raise_for_status()

    data = resp.json()
    return _parse_send_response(data)


def _parse_send_response(data: dict[str, Any]) -> A2ASendMessageResponse:
    """Normalise a /message:send response — handles both raw Task and envelope."""
    # JSON-RPC envelope: {"jsonrpc": "2.0", "result": { ...task... }}
    task_data = data.get("result", data)

    try:
        task = A2ATask.model_validate(task_data)
    except Exception:
        logger.warning("send_message: could not parse task from response, returning raw")
        return A2ASendMessageResponse(raw=data)

    return A2ASendMessageResponse(task=task, raw=data)


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
