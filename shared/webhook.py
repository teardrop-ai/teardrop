# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared GET-webhook invocation helper for tool integrations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp


@dataclass(slots=True)
class WebhookCallResult:
    status_code: int
    content_type: str
    body: bytes


class WebhookCallError(Exception):
    """Structured error for expected webhook call failures."""

    def __init__(self, message: str, error_type: str):
        super().__init__(message)
        self.message = message
        self.error_type = error_type


class WebhookCaller:
    """Perform SSRF-safe authenticated GET webhook calls."""

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: int,
        auth_header_name: str | None,
        auth_header_encrypted: str | None,
        max_response_bytes: int,
    ):
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._auth_header_name = auth_header_name
        self._auth_header_encrypted = auth_header_encrypted
        self._max_response_bytes = max_response_bytes

    async def call_get(
        self,
        *,
        params: dict[str, Any],
        decrypt_header: Callable[[str], str],
        validate_url: Callable[[str], Awaitable[tuple[str | None, list[str]]]],
        client_session_factory: Callable[..., Any] | None = None,
        connector_factory: Callable[[str, list[str]], Any] | None = None,
    ) -> WebhookCallResult:
        """Execute a GET webhook call and return raw response details.

        ``validate_url`` returns ``(error, resolved_ips)``. When ``error`` is
        None the call proceeds and, if ``connector_factory`` is supplied, the
        connection is pinned to ``resolved_ips`` — closing the DNS-rebinding
        TOCTOU window between validation and the actual TCP connect.
        """
        from urllib.parse import urlparse

        url_error, resolved_ips = await validate_url(self._url)
        if url_error is not None:
            raise WebhookCallError(f"Webhook URL blocked: {url_error}", "ssrf_blocked")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_header_name and self._auth_header_encrypted:
            try:
                headers[self._auth_header_name] = decrypt_header(self._auth_header_encrypted)
            except Exception:
                raise WebhookCallError("Failed to decrypt webhook auth header", "decrypt_failure")

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        session_factory = client_session_factory or aiohttp.ClientSession
        session_kwargs: dict[str, Any] = {"timeout": timeout}
        if connector_factory is not None and resolved_ips:
            hostname = urlparse(self._url).hostname or ""
            session_kwargs["connector"] = connector_factory(hostname, resolved_ips)
        try:
            async with session_factory(**session_kwargs) as session:
                resp = await session.get(self._url, headers=headers, params=params)
                body = await resp.read()
                if len(body) > self._max_response_bytes:
                    body = body[: self._max_response_bytes]

                return WebhookCallResult(
                    status_code=resp.status,
                    content_type=resp.headers.get("Content-Type", ""),
                    body=body,
                )
        except asyncio.TimeoutError:
            raise WebhookCallError(f"Webhook timed out after {self._timeout_seconds}s", "timeout")
        except aiohttp.ClientError as exc:
            error_name = type(exc).__name__
            raise WebhookCallError(f"Webhook request failed: {error_name}", error_name)

