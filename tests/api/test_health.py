"""API tests for / and /health endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.anyio
async def test_health_ok(api_client):
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "teardrop"
    assert "version" in body
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["permissions-policy"] == "geolocation=(), microphone=()"


@pytest.mark.anyio
async def test_root_redirects_to_docs(api_client):
    resp = await api_client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/docs" in resp.headers.get("location", "")


@pytest.mark.anyio
async def test_agent_card_shape(api_client):
    resp = await api_client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Teardrop"
    assert body["protocolVersion"] == "1.0"
    assert "skills" in body
    assert "tools" in body
    assert "authentication" in body
    assert "securitySchemes" in body
    assert body["supportedInterfaces"][0]["url"] == "http://test/agent/run"
    assert body["supportedInterfaces"][1]["url"] == "http://test/message:send"
    assert body["defaultInputModes"] == ["text/plain", "application/json"]
    assert all("id" in skill for skill in body["skills"])
    assert body["endpoints"]["a2a_message"] == "/message:send"
    assert body["endpoints"]["mcp_tools"] == "/tools/mcp"
    assert body["capabilities"]["billing"]["pricing_endpoint"] == "/billing/pricing"


@pytest.mark.anyio
async def test_agent_card_marketplace_discovery(api_client, test_settings):
    test_settings.marketplace_enabled = True

    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["capabilities"]["marketplace"] == {
        "enabled": True,
        "catalog_endpoint": "/marketplace/catalog",
        "mcp_gateway_endpoint": "/tools/mcp",
    }
    assert body["endpoints"]["marketplace_catalog"] == "/marketplace/catalog"


@pytest.mark.anyio
async def test_agent_card_omits_marketplace_when_disabled(api_client, test_settings):
    test_settings.marketplace_enabled = False

    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert "marketplace" not in body["capabilities"]
    assert "marketplace_catalog" not in body["endpoints"]


@pytest.mark.anyio
async def test_agent_card_omits_inbound_a2a_when_disabled(api_client, test_settings):
    test_settings.a2a_inbound_enabled = False

    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["supportedInterfaces"] == [
        {
            "url": "http://test/agent/run",
            "protocolBinding": "https://teardrop.ai/bindings/ag-ui-sse/v1",
            "protocolVersion": "1.0",
        }
    ]
    assert "a2a_message" not in body["endpoints"]
    assert body["protocols"] == ["ag-ui", "mcp"]


@pytest.mark.anyio
async def test_agent_card_prefers_app_base_url(api_client, test_settings):
    test_settings.app_base_url = "https://api.teardrop.dev"

    resp = await api_client.get(
        "/.well-known/agent-card.json",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "ignored.example.com",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://api.teardrop.dev"
    assert body["documentationUrl"] == "https://api.teardrop.dev/docs"
    assert body["supportedInterfaces"][0]["url"] == "https://api.teardrop.dev/agent/run"
    assert body["supportedInterfaces"][1]["url"] == "https://api.teardrop.dev/message:send"


@pytest.mark.anyio
async def test_agent_card_falls_back_to_forwarded_host(api_client, test_settings):
    test_settings.app_base_url = ""

    resp = await api_client.get(
        "/.well-known/agent-card.json",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "proxy.teardrop.dev",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["url"] == "https://proxy.teardrop.dev"


@pytest.mark.anyio
async def test_agent_card_headers_and_legacy_alias(api_client):
    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert "etag" in resp.headers
    assert resp.headers["vary"] == "Host, X-Forwarded-Host, X-Forwarded-Proto"

    legacy_resp = await api_client.get("/.well-known/agent.json")
    assert legacy_resp.status_code == 200
    assert legacy_resp.json() == resp.json()

    cached_resp = await api_client.get(
        "/.well-known/agent-card.json",
        headers={"If-None-Match": resp.headers["etag"]},
    )
    assert cached_resp.status_code == 304


@pytest.mark.anyio
async def test_mcp_server_card(api_client, test_settings):
    test_settings.agent_card_icon_url = "https://example.com/icon.png"
    resp = await api_client.get("/.well-known/mcp/server-card.json")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert "etag" in resp.headers

    body = resp.json()
    assert body["title"] == "Teardrop"
    assert body["description"].startswith("The native infrastructure layer")
    assert body["homepage"] == "http://test"
    assert body["documentationUrl"] == "http://test/docs"
    assert body["iconUrl"] == "https://example.com/icon.png"
    assert body["serverInfo"]["title"] == "Teardrop"
    assert body["serverInfo"]["websiteUrl"] == "http://test"
    assert body["serverInfo"]["icons"] == [{"src": "https://example.com/icon.png"}]

    # Check that tools have outputSchema, annotations, title
    tools = body["tools"]
    assert len(tools) > 0
    t = tools[0]
    assert "title" in t
    assert "inputSchema" in t
    assert "outputSchema" in t
    assert "annotations" in t


@pytest.mark.anyio
async def test_oauth_protected_resource_metadata(api_client):
    root_resp = await api_client.get("/.well-known/oauth-protected-resource")

    assert root_resp.status_code == 200
    assert root_resp.headers["cache-control"] == "public, max-age=300"
    assert "etag" in root_resp.headers
    root_body = root_resp.json()
    assert root_body["resource"] == "http://test"
    assert root_body["resource_name"] == "Teardrop"
    assert root_body["resource_documentation"] == "http://test/docs"
    assert root_body["bearer_methods_supported"] == ["header"]
    assert root_body["homepage"] == "http://test"

    mcp_resp = await api_client.get("/.well-known/oauth-protected-resource/tools/mcp")

    assert mcp_resp.status_code == 200
    mcp_body = mcp_resp.json()
    assert mcp_body["resource"] == "http://test/tools/mcp"
    assert mcp_body["resource_name"] == "Teardrop MCP"
    assert mcp_body["resource_documentation"] == "http://test/docs"

    cached_resp = await api_client.get(
        "/.well-known/oauth-protected-resource/tools/mcp",
        headers={"If-None-Match": mcp_resp.headers["etag"]},
    )
    assert cached_resp.status_code == 304


@pytest.mark.anyio
async def test_root_llms_txt(api_client, test_settings):
    test_settings.marketplace_enabled = True

    resp = await api_client.get("/llms.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert "# Teardrop" in resp.text
    assert "http://test/.well-known/agent-card.json" in resp.text
    assert "http://test/marketplace/llms.txt" in resp.text


@pytest.mark.anyio
async def test_root_robots_txt(api_client):
    resp = await api_client.get("/robots.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert "User-agent: *" in resp.text
    assert "http://test/llms.txt" in resp.text
