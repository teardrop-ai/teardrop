# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared provenance metadata for externally sourced tool results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

_UNSET = object()


class DataProvenance(BaseModel):
    """Machine-readable source and freshness metadata for a tool response."""

    provider: str
    source_urls: list[str] = Field(default_factory=list)
    retrieved_at: str
    source_fetched_at: str | None = None
    cache_hit: bool = False
    cache_age_seconds: float | None = Field(default=None, ge=0)
    cache_ttl_seconds: int | None = Field(default=None, ge=0)


def utc_now_iso() -> str:
    """Return the current UTC time in a stable ISO-8601 representation."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def cache_age_seconds(source_fetched_at: str | None) -> float | None:
    """Return the age of a source timestamp, or None when it is unavailable."""
    if not source_fetched_at:
        return None
    try:
        fetched = datetime.fromisoformat(source_fetched_at.replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return round(max(0.0, (datetime.now(timezone.utc) - fetched).total_seconds()), 3)
    except (TypeError, ValueError):
        return None


def _normalize_source_timestamp(source_fetched_at: str | None | object) -> str | None:
    if not isinstance(source_fetched_at, str):
        return None
    try:
        datetime.fromisoformat(source_fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return source_fetched_at


def build_provenance(
    provider: str,
    source_urls: list[str],
    *,
    cache_hit: bool = False,
    source_fetched_at: str | None | object = _UNSET,
    cache_age: float | None = None,
    cache_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe provenance object for a fresh or cached response."""
    retrieved_at = utc_now_iso()
    if source_fetched_at is _UNSET:
        source_fetched_at = retrieved_at
    else:
        source_fetched_at = _normalize_source_timestamp(source_fetched_at)
    return DataProvenance(
        provider=provider,
        source_urls=source_urls,
        retrieved_at=retrieved_at,
        source_fetched_at=source_fetched_at,
        cache_hit=cache_hit,
        cache_age_seconds=cache_age,
        cache_ttl_seconds=cache_ttl_seconds,
    ).model_dump()


def attach_provenance(
    result: dict[str, Any],
    provider: str,
    source_urls: list[str],
    *,
    cache_hit: bool = False,
    source_fetched_at: str | None | object = _UNSET,
    cache_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Return a result copy with standardized provenance metadata attached."""
    existing = result.get("provenance")
    existing_provenance = existing if isinstance(existing, dict) else None
    if cache_hit:
        if source_fetched_at is _UNSET:
            source_fetched_at = existing_provenance.get("source_fetched_at") if existing_provenance else None
        if not source_urls and existing_provenance:
            existing_urls = existing_provenance.get("source_urls")
            if isinstance(existing_urls, list) and all(isinstance(url, str) for url in existing_urls):
                source_urls = existing_urls
        if cache_ttl_seconds is None and existing_provenance:
            existing_ttl = existing_provenance.get("cache_ttl_seconds")
            if isinstance(existing_ttl, int) and existing_ttl >= 0:
                cache_ttl_seconds = existing_ttl
    age = cache_age_seconds(source_fetched_at if isinstance(source_fetched_at, str) else None) if cache_hit else None
    output = dict(result)
    output["provenance"] = build_provenance(
        provider,
        source_urls,
        cache_hit=cache_hit,
        source_fetched_at=source_fetched_at,
        cache_age=age,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    return output
