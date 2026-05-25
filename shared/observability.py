# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Sentry observability wiring for Teardrop.

Quota-frugal by design — the Developer (free) tier allows 5k errors/month,
so we explicitly:

* Disable transaction/profile sampling (errors only).
* Override the ``LoggingIntegration`` default ``event_level=ERROR`` to
  ``CRITICAL`` so existing ``logger.error()`` / ``logger.exception()`` calls
  do **not** auto-forward as events. All real events come from explicit
  ``sentry_sdk.capture_exception`` calls in critical paths.
* Scrub Teardrop-specific sensitive headers and extras in ``before_send``.

Empty DSN → SDK is never initialized (no network, zero overhead).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Headers and extras that must never reach Sentry. Lower-cased for matching.
_SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "stripe-signature",
        "x-payment",
        "x-payment-response",
        "x-refresh-token",
        "x-csrf-token",
    }
)

_SENSITIVE_EXTRA_KEYS: frozenset[str] = frozenset(
    {
        "payment_payload",
        "payment_header",
        "signature",
        "private_key",
        "client_secret",
        "api_key",
        "refresh_token",
        "access_token",
        "siwe_signature",
        "raw_body",
        "stripe_webhook_secret",
    }
)

_FILTERED = "[Filtered]"


def _scrub_mapping(data: Any, sensitive_keys: frozenset[str]) -> None:
    """Recursively replace sensitive values in a dict/list with ``[Filtered]``.

    Mutates in place. Bounded depth via natural recursion on event payload
    shape (Sentry events are not deeply nested).
    """
    if isinstance(data, dict):
        for key in list(data.keys()):
            if isinstance(key, str) and key.lower() in sensitive_keys:
                data[key] = _FILTERED
            else:
                _scrub_mapping(data[key], sensitive_keys)
    elif isinstance(data, list):
        for item in data:
            _scrub_mapping(item, sensitive_keys)


def _before_send(event: dict, hint: dict) -> dict | None:  # noqa: ARG001 - hint required by API
    """Strip Teardrop-specific sensitive fields before transmission."""
    try:
        request = event.get("request")
        if isinstance(request, dict):
            headers = request.get("headers")
            if isinstance(headers, dict):
                for header_key in list(headers.keys()):
                    if isinstance(header_key, str) and header_key.lower() in _SENSITIVE_HEADERS:
                        headers[header_key] = _FILTERED
            # Drop raw bodies entirely — they may contain payment payloads.
            if "data" in request:
                request["data"] = _FILTERED

        for section in ("extra", "contexts", "tags"):
            payload = event.get(section)
            if payload is not None:
                _scrub_mapping(payload, _SENSITIVE_EXTRA_KEYS)

        # Scrub stack-frame locals which can capture function arguments.
        for exc in (event.get("exception", {}) or {}).get("values", []) or []:
            stacktrace = exc.get("stacktrace") if isinstance(exc, dict) else None
            if not isinstance(stacktrace, dict):
                continue
            for frame in stacktrace.get("frames", []) or []:
                vars_ = frame.get("vars") if isinstance(frame, dict) else None
                if isinstance(vars_, dict):
                    _scrub_mapping(vars_, _SENSITIVE_EXTRA_KEYS)
    except Exception:  # pragma: no cover - defensive: never let scrubbing crash
        logger.exception("sentry before_send scrubber raised; sending event anyway")
    return event


def init_sentry(settings: Any) -> bool:
    """Initialize the Sentry SDK if a DSN is configured.

    Returns ``True`` if Sentry was initialized, ``False`` otherwise. Safe to
    call multiple times — only the first call with a non-empty DSN takes
    effect (subsequent ``sentry_sdk.init`` calls overwrite the client; we
    guard via the DSN check).
    """
    dsn = (getattr(settings, "sentry_dsn", "") or "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.warning("sentry_sdk not installed; observability disabled")
        return False

    environment = (getattr(settings, "sentry_environment", "") or "").strip() or getattr(settings, "app_env", "development")
    release = os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_COMMIT") or "dev"

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        # Errors only — no transaction/profile quota burn.
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        # Default scrubber + our custom hook handle PII; do not auto-attach.
        send_default_pii=False,
        attach_stacktrace=False,
        max_breadcrumbs=30,
        before_send=_before_send,
        integrations=[
            # Override default LoggingIntegration so only CRITICAL logs become
            # events. logger.error/.exception calls remain breadcrumbs only —
            # this is the #1 quota-saving lever.
            LoggingIntegration(
                level=logging.WARNING,
                event_level=logging.CRITICAL,
            ),
        ],
    )
    logger.info("Sentry initialized environment=%s release=%s", environment, release)
    return True
