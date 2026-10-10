# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""API tests for PUT /llm-config — api_base BYOK guard (security).

A custom ``api_base`` with no BYOK key would forward the platform's shared
provider key to an arbitrary org-controlled endpoint. These tests lock in the
422 guard that requires BYOK whenever ``api_base`` is supplied.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


def _fake_cfg(**overrides):
    """Build a stand-in OrgLlmConfig with isoformat-capable timestamps."""
    now = datetime(2026, 5, 31, tzinfo=timezone.utc)
    cfg = MagicMock()
    cfg.org_id = "test-org-id"
    cfg.provider = overrides.get("provider", "openai")
    cfg.model = overrides.get("model", "gpt-4o")
    cfg.has_api_key = overrides.get("has_api_key", True)
    cfg.api_base = overrides.get("api_base", "https://proxy.example.com/v1")
    cfg.max_tokens = 4096
    cfg.temperature = 0.0
    cfg.timeout_seconds = 120
    cfg.routing_preference = "default"
    cfg.reasoning_effort = overrides.get("reasoning_effort")
    cfg.model_reasoning_effort = overrides.get("model_reasoning_effort", {})
    cfg.is_byok = overrides.get("is_byok", True)
    cfg.created_at = now
    cfg.updated_at = now
    return cfg


@pytest.mark.anyio
async def test_api_base_without_api_key_returns_422(api_client, monkeypatch):
    """api_base with no BYOK key (and no stored key) is rejected."""
    monkeypatch.setattr(
        "teardrop.routers.org.llm_config.get_org_llm_config",
        AsyncMock(return_value=None),
    )

    resp = await api_client.put(
        "/llm-config",
        json={
            "provider": "openai",
            "model": "gpt-4o",  # in MODEL_CATALOGUE so the catalogue gate passes
            "api_base": "https://attacker.example.com/v1",
        },
    )
    assert resp.status_code == 422
    assert "api_base requires api_key" in resp.json()["detail"]


@pytest.mark.anyio
async def test_api_base_with_api_key_accepted(api_client, monkeypatch):
    """api_base with a BYOK key in the request is accepted."""
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda url: None)
    monkeypatch.setattr(
        "teardrop.routers.org.llm_config.upsert_org_llm_config",
        AsyncMock(return_value=_fake_cfg()),
    )

    resp = await api_client.put(
        "/llm-config",
        json={
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "sk-byok-secret",
            "api_base": "https://proxy.example.com/v1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["has_api_key"] is True


@pytest.mark.anyio
async def test_api_base_with_existing_stored_key_accepted(api_client, monkeypatch):
    """api_base accepted when api_key omitted but a stored BYOK key is preserved."""
    monkeypatch.setattr("tools.definitions.http_fetch.validate_url", lambda url: None)
    monkeypatch.setattr(
        "teardrop.routers.org.llm_config.get_org_llm_config",
        AsyncMock(return_value=_fake_cfg(has_api_key=True)),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.llm_config.upsert_org_llm_config",
        AsyncMock(return_value=_fake_cfg()),
    )

    resp = await api_client.put(
        "/llm-config",
        json={
            "provider": "openai",
            "model": "gpt-4o",
            "api_base": "https://proxy.example.com/v1",
        },
    )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_api_base_with_explicit_null_key_returns_422(api_client, monkeypatch):
    """Explicitly clearing the BYOK key while setting api_base is rejected."""
    monkeypatch.setattr(
        "teardrop.routers.org.llm_config.get_org_llm_config",
        AsyncMock(return_value=_fake_cfg(has_api_key=True)),
    )

    resp = await api_client.put(
        "/llm-config",
        json={
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": None,
            "api_base": "https://attacker.example.com/v1",
        },
    )
    assert resp.status_code == 422
    assert "api_base requires api_key" in resp.json()["detail"]


@pytest.mark.anyio
async def test_no_api_base_without_api_key_accepted(api_client, monkeypatch):
    """Regression: a normal non-BYOK config (no api_base) is still accepted."""
    monkeypatch.setattr(
        "teardrop.routers.org.llm_config.upsert_org_llm_config",
        AsyncMock(return_value=_fake_cfg(provider="openai", model="gpt-4o", has_api_key=False, is_byok=False, api_base=None)),
    )

    resp = await api_client.put(
        "/llm-config",
        json={
            "provider": "openai",
            "model": "gpt-4o",
        },
    )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_reasoning_effort_round_trips(api_client, monkeypatch):
    """Org-wide and per-model reasoning effort are normalised and persisted."""
    efforts = {"openrouter:~anthropic/claude-opus-latest": "high"}
    upsert = AsyncMock(return_value=_fake_cfg(reasoning_effort="low", model_reasoning_effort=efforts, api_base=None))
    monkeypatch.setattr("teardrop.routers.org.llm_config.upsert_org_llm_config", upsert)

    resp = await api_client.put(
        "/llm-config",
        json={
            "provider": "openai",
            "model": "gpt-4o",
            "reasoning_effort": " LOW ",
            "model_reasoning_effort": {"OpenRouter:~anthropic/claude-opus-latest": "High"},
        },
    )
    assert resp.status_code == 200
    kwargs = upsert.call_args.kwargs
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["model_reasoning_effort"] == efforts
    body = resp.json()
    assert body["reasoning_effort"] == "low"
    assert body["model_reasoning_effort"] == efforts


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"reasoning_effort": "extreme"},
        {"model_reasoning_effort": {"openrouter:a/b": "max"}},
        {"model_reasoning_effort": {"no-provider-prefix": "low"}},
        {"model_reasoning_effort": {"anthropic:claude": "low"}},
    ],
)
async def test_invalid_reasoning_effort_returns_422(api_client, monkeypatch, payload):
    upsert = AsyncMock(return_value=_fake_cfg())
    monkeypatch.setattr("teardrop.routers.org.llm_config.upsert_org_llm_config", upsert)

    resp = await api_client.put("/llm-config", json={"provider": "openai", "model": "gpt-4o", **payload})
    assert resp.status_code == 422
    upsert.assert_not_called()
