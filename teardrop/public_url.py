# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Helpers for deriving the public-facing application base URL."""

from __future__ import annotations

from fastapi import Request

from teardrop.config import Settings


def first_forwarded_value(value: str | None) -> str:
    if not value:
        return ""
    return value.split(",", 1)[0].strip()


def public_base_url(request: Request, current_settings: Settings) -> str:
    configured_value = getattr(current_settings, "app_base_url", "")
    configured = configured_value.strip().rstrip("/") if isinstance(configured_value, str) else ""
    if configured:
        return configured

    forwarded_proto = first_forwarded_value(request.headers.get("x-forwarded-proto"))
    forwarded_host = first_forwarded_value(request.headers.get("x-forwarded-host"))
    scheme = forwarded_proto or request.url.scheme
    host = forwarded_host or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")
