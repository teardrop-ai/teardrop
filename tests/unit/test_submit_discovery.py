from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import submit_discovery


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_validate_agent_card_accepts_additive_card():
    errors = submit_discovery.validate_agent_card(
        {
            "name": "Teardrop",
            "description": "desc",
            "version": "1.0.0",
            "url": "https://api.teardrop.dev",
            "capabilities": {},
            "skills": [{"id": "task_planning", "name": "task_planning", "description": "desc"}],
            "supportedInterfaces": [
                {
                    "url": "https://api.teardrop.dev/agent/run",
                    "protocolBinding": "https://teardrop.ai/bindings/ag-ui-sse/v1",
                    "protocolVersion": "1.0",
                }
            ],
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "securitySchemes": {"bearer_jwt": {}},
        }
    )

    assert errors == []


def test_validate_agent_card_requires_skill_ids():
    errors = submit_discovery.validate_agent_card(
        {
            "name": "Teardrop",
            "description": "desc",
            "version": "1.0.0",
            "url": "https://api.teardrop.dev",
            "capabilities": {},
            "skills": [{"name": "task_planning"}],
            "supportedInterfaces": [{"url": "https://api.teardrop.dev", "protocolBinding": "binding", "protocolVersion": "1.0"}],
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "securitySchemes": {"bearer_jwt": {}},
        }
    )

    assert any("skills[0] missing id" in error for error in errors)


def test_validate_llms_txt_requires_title_and_links():
    errors = submit_discovery.validate_llms_txt("No markdown links here")

    assert any("H1" in error for error in errors)
    assert any("markdown link" in error for error in errors)


@pytest.mark.anyio
async def test_validate_registry_urls_rejects_ssrf(monkeypatch):
    async def _blocked(_url: str) -> str | None:
        return "blocked host"

    monkeypatch.setattr(submit_discovery, "async_validate_url", _blocked)

    with pytest.raises(ValueError, match="blocked host"):
        await submit_discovery.validate_registry_urls(["https://registry.example.com"])


@pytest.mark.anyio
async def test_submit_payloads_respects_dry_run():
    client = AsyncMock()

    results = await submit_discovery.submit_payloads(
        client=client,
        registry_urls=["https://registry.example.com"],
        payload={"name": "Teardrop"},
        dry_run=True,
    )

    assert results == [{"url": "https://registry.example.com", "submitted": False, "dry_run": True}]
    client.post.assert_not_called()


@pytest.mark.anyio
async def test_submit_payloads_posts_payload():
    response = MagicMock()
    response.status_code = 201
    response.raise_for_status.return_value = None
    client = AsyncMock()
    client.post.return_value = response

    results = await submit_discovery.submit_payloads(
        client=client,
        registry_urls=["https://registry.example.com"],
        payload={"name": "Teardrop"},
        dry_run=False,
    )

    assert results == [{"url": "https://registry.example.com", "submitted": True, "status_code": 201}]
    client.post.assert_awaited_once_with("https://registry.example.com", json={"name": "Teardrop"})
