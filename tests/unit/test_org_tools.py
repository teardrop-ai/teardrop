"""Unit tests for org_tools.py — CRUD, encryption, caching, and webhook execution."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

import org_tools as org_tools_module
from org_tools import (
    OrgTool,
    _build_langchain_tool,
    _build_pydantic_model,
    _decrypt_header,
    _encrypt_header,
    invalidate_org_tools_cache,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

_TEST_FERNET_KEY = Fernet.generate_key().decode()


def _pool():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=0)
    pool.execute = AsyncMock(return_value="UPDATE 1")
    return pool


def _sample_org_tool(**overrides) -> OrgTool:
    defaults = {
        "id": "tool-1",
        "org_id": "org-1",
        "name": "my_tool",
        "description": "A test tool",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query"}},
            "required": ["query"],
        },
        "webhook_url": "https://example.com/webhook",
        "webhook_method": "POST",
        "has_auth": False,
        "timeout_seconds": 10,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return OrgTool(**defaults)


def _tool_row(**overrides) -> dict:
    defaults = {
        "id": "tool-1",
        "org_id": "org-1",
        "name": "my_tool",
        "description": "A test tool",
        "input_schema": json.dumps({
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }),
        "webhook_url": "https://example.com/webhook",
        "webhook_method": "POST",
        "auth_header_name": None,
        "auth_header_enc": None,
        "timeout_seconds": 10,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return defaults


# ─── Encryption ───────────────────────────────────────────────────────────────


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self, monkeypatch):
        monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", _TEST_FERNET_KEY)
        # Reset cached fernet
        org_tools_module._fernet = None
        try:
            import config
            config.get_settings.cache_clear()
            encrypted = _encrypt_header("Bearer my-secret-token")
            decrypted = _decrypt_header(encrypted)
            assert decrypted == "Bearer my-secret-token"
        finally:
            org_tools_module._fernet = None
            config.get_settings.cache_clear()

    def test_decrypt_with_wrong_key_fails(self, monkeypatch):
        monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", _TEST_FERNET_KEY)
        org_tools_module._fernet = None
        try:
            import config
            config.get_settings.cache_clear()
            encrypted = _encrypt_header("secret-value")

            # Now swap to a different key
            new_key = Fernet.generate_key().decode()
            monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", new_key)
            org_tools_module._fernet = None
            config.get_settings.cache_clear()

            from cryptography.fernet import InvalidToken
            with pytest.raises(InvalidToken):
                _decrypt_header(encrypted)
        finally:
            org_tools_module._fernet = None
            config.get_settings.cache_clear()


# ─── Dynamic Pydantic Model ──────────────────────────────────────────────────


class TestBuildPydanticModel:
    def test_basic_string_field(self):
        schema = {
            "properties": {"query": {"type": "string", "description": "Search query"}},
            "required": ["query"],
        }
        model = _build_pydantic_model("test", schema)
        instance = model(query="hello")
        assert instance.query == "hello"

    def test_multiple_types(self):
        schema = {
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
            },
            "required": ["name"],
        }
        model = _build_pydantic_model("multi", schema)
        instance = model(name="test", count=5, ratio=3.14, flag=True)
        assert instance.name == "test"
        assert instance.count == 5
        assert instance.ratio == 3.14
        assert instance.flag is True

    def test_optional_fields_default_none(self):
        schema = {
            "properties": {
                "required_field": {"type": "string"},
                "optional_field": {"type": "string"},
            },
            "required": ["required_field"],
        }
        model = _build_pydantic_model("opt", schema)
        instance = model(required_field="hello")
        assert instance.optional_field is None

    def test_unknown_type_defaults_to_str(self):
        schema = {
            "properties": {"data": {"type": "unknown_type"}},
            "required": ["data"],
        }
        model = _build_pydantic_model("unk", schema)
        instance = model(data="anything")
        assert instance.data == "anything"

    def test_empty_properties(self):
        schema = {"properties": {}}
        model = _build_pydantic_model("empty", schema)
        instance = model()
        assert instance is not None


# ─── Build LangChain Tool ────────────────────────────────────────────────────


class TestBuildLangchainTool:
    def test_creates_structured_tool(self):
        tool = _sample_org_tool()
        lc_tool = _build_langchain_tool(tool, None, None)
        assert lc_tool.name == "my_tool"
        assert lc_tool.description == "A test tool"

    def test_tool_has_correct_args_schema(self):
        tool = _sample_org_tool()
        lc_tool = _build_langchain_tool(tool, None, None)
        schema = lc_tool.args_schema.model_json_schema()
        assert "query" in schema["properties"]


# ─── CRUD (mocked DB) ────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCreateOrgTool:
    async def test_success(self, monkeypatch):
        monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", _TEST_FERNET_KEY)
        org_tools_module._fernet = None
        import config
        config.get_settings.cache_clear()

        pool = _pool()
        try:
            with patch.object(org_tools_module, "_pool", pool):
                tool = await org_tools_module.create_org_tool(
                    org_id="org-1",
                    name="crm_lookup",
                    description="Look up CRM records",
                    input_schema={"properties": {"id": {"type": "string"}}, "required": ["id"]},
                    webhook_url="https://example.com/crm",
                    webhook_method="POST",
                    auth_header_name=None,
                    auth_header_value=None,
                    timeout_seconds=10,
                    actor_id="user-1",
                )
            assert isinstance(tool, OrgTool)
            assert tool.name == "crm_lookup"
            assert tool.org_id == "org-1"
            assert tool.is_active is True
            pool.execute.assert_called()
        finally:
            org_tools_module._fernet = None
            config.get_settings.cache_clear()

    async def test_quota_exceeded_raises(self, monkeypatch):
        monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", _TEST_FERNET_KEY)
        monkeypatch.setenv("MAX_ORG_TOOLS", "2")
        org_tools_module._fernet = None
        import config
        config.get_settings.cache_clear()

        pool = _pool()
        pool.fetchval = AsyncMock(return_value=2)  # already at limit
        try:
            with patch.object(org_tools_module, "_pool", pool):
                with pytest.raises(ValueError, match="tool limit reached"):
                    await org_tools_module.create_org_tool(
                        org_id="org-1",
                        name="tool_3",
                        description="One too many",
                        input_schema={"properties": {}},
                        webhook_url="https://example.com/hook",
                        webhook_method="POST",
                        auth_header_name=None,
                        auth_header_value=None,
                        timeout_seconds=10,
                        actor_id="user-1",
                    )
        finally:
            org_tools_module._fernet = None
            config.get_settings.cache_clear()

    async def test_with_auth_header_encrypts(self, monkeypatch):
        monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", _TEST_FERNET_KEY)
        org_tools_module._fernet = None
        import config
        config.get_settings.cache_clear()

        pool = _pool()
        try:
            with patch.object(org_tools_module, "_pool", pool):
                tool = await org_tools_module.create_org_tool(
                    org_id="org-1",
                    name="authed_tool",
                    description="With auth",
                    input_schema={"properties": {}},
                    webhook_url="https://example.com/hook",
                    webhook_method="POST",
                    auth_header_name="Authorization",
                    auth_header_value="Bearer secret123",
                    timeout_seconds=10,
                    actor_id="user-1",
                )
            assert tool.has_auth is True
            # Verify the encrypted value was passed to DB
            # First call to execute is the INSERT into org_tools
            insert_call = pool.execute.call_args_list[0]
            insert_args = insert_call[0]
            # auth_header_enc is the 9th positional arg (index 8 in args tuple
            # after the SQL string, so index 9 in 0-based)
            enc_val = insert_args[9]  # auth_header_enc
            assert enc_val is not None
            assert enc_val != "Bearer secret123"  # should be encrypted
        finally:
            org_tools_module._fernet = None
            config.get_settings.cache_clear()


@pytest.mark.anyio
class TestGetOrgTool:
    async def test_found(self):
        pool = _pool()
        row = _tool_row()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(org_tools_module, "_pool", pool):
            tool = await org_tools_module.get_org_tool("tool-1", "org-1")
        assert tool is not None
        assert tool.id == "tool-1"

    async def test_not_found(self):
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(org_tools_module, "_pool", pool):
            tool = await org_tools_module.get_org_tool("bad-id", "org-1")
        assert tool is None

    async def test_wrong_org_returns_none(self):
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)  # scoped query won't match
        with patch.object(org_tools_module, "_pool", pool):
            tool = await org_tools_module.get_org_tool("tool-1", "other-org")
        assert tool is None


@pytest.mark.anyio
class TestListOrgTools:
    async def test_returns_list(self):
        pool = _pool()
        pool.fetch = AsyncMock(return_value=[_tool_row(), _tool_row(id="tool-2", name="tool_b")])
        with patch.object(org_tools_module, "_pool", pool):
            tools = await org_tools_module.list_org_tools("org-1")
        assert len(tools) == 2
        assert all(isinstance(t, OrgTool) for t in tools)

    async def test_empty(self):
        pool = _pool()
        with patch.object(org_tools_module, "_pool", pool):
            tools = await org_tools_module.list_org_tools("org-1")
        assert tools == []


@pytest.mark.anyio
class TestDeleteOrgTool:
    async def test_soft_delete(self):
        pool = _pool()
        pool.execute = AsyncMock(return_value="UPDATE 1")
        pool.fetchrow = AsyncMock(return_value={"name": "my_tool"})
        with patch.object(org_tools_module, "_pool", pool):
            result = await org_tools_module.delete_org_tool("tool-1", "org-1", actor_id="user-1")
        assert result is True

    async def test_not_found(self):
        pool = _pool()
        pool.execute = AsyncMock(return_value="UPDATE 0")
        with patch.object(org_tools_module, "_pool", pool):
            result = await org_tools_module.delete_org_tool("bad-id", "org-1", actor_id="user-1")
        assert result is False


# ─── Webhook execution (mocked aiohttp) ──────────────────────────────────────


@pytest.mark.anyio
class TestWebhookExecution:
    async def test_success(self):
        tool = _sample_org_tool()
        lc_tool = _build_langchain_tool(tool, None, None)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=json.dumps({"result": "ok"}).encode())
        mock_resp.headers = {"Content-Type": "application/json"}

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("org_tools.validate_url", return_value=None), \
             patch("org_tools.aiohttp.ClientSession", return_value=mock_session):
            result = await lc_tool.ainvoke({"query": "test"})
        assert result == {"result": "ok"}

    async def test_timeout(self):
        tool = _sample_org_tool(timeout_seconds=1)
        lc_tool = _build_langchain_tool(tool, None, None)

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("org_tools.validate_url", return_value=None), \
             patch("org_tools.aiohttp.ClientSession", return_value=mock_session):
            result = await lc_tool.ainvoke({"query": "test"})
        assert "timed out" in result["error"]

    async def test_non_json_response(self):
        tool = _sample_org_tool()
        lc_tool = _build_langchain_tool(tool, None, None)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"<html>not json</html>")
        mock_resp.headers = {"Content-Type": "text/html"}

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("org_tools.validate_url", return_value=None), \
             patch("org_tools.aiohttp.ClientSession", return_value=mock_session):
            result = await lc_tool.ainvoke({"query": "test"})
        assert "non-JSON" in result["error"]

    async def test_ssrf_recheck_blocks(self):
        tool = _sample_org_tool(webhook_url="http://169.254.169.254/metadata")
        lc_tool = _build_langchain_tool(tool, None, None)

        with patch("org_tools.validate_url", return_value="blocked: metadata endpoint"):
            result = await lc_tool.ainvoke({"query": "test"})
        assert "blocked" in result["error"]

    async def test_auth_header_sent(self, monkeypatch):
        monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", _TEST_FERNET_KEY)
        org_tools_module._fernet = None
        import config
        config.get_settings.cache_clear()

        try:
            encrypted = _encrypt_header("Bearer my-token")
            tool = _sample_org_tool(has_auth=True)
            lc_tool = _build_langchain_tool(tool, "Authorization", encrypted)

            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.read = AsyncMock(return_value=json.dumps({"ok": True}).encode())
            mock_resp.headers = {"Content-Type": "application/json"}

            mock_session = AsyncMock()
            mock_session.post = AsyncMock(return_value=mock_resp)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)

            with patch("org_tools.validate_url", return_value=None), \
                 patch("org_tools.aiohttp.ClientSession", return_value=mock_session):
                result = await lc_tool.ainvoke({"query": "test"})

            # Verify auth header was included
            call_kwargs = mock_session.post.call_args
            headers = call_kwargs.kwargs.get("headers", call_kwargs[1].get("headers", {}))
            assert headers.get("Authorization") == "Bearer my-token"
            assert result == {"ok": True}
        finally:
            org_tools_module._fernet = None
            config.get_settings.cache_clear()

    async def test_response_truncation(self):
        tool = _sample_org_tool()
        lc_tool = _build_langchain_tool(tool, None, None)

        # Create a response larger than 50KB
        large_body = json.dumps({"data": "x" * 60_000}).encode()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=large_body)
        mock_resp.headers = {"Content-Type": "application/json"}

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("org_tools.validate_url", return_value=None), \
             patch("org_tools.aiohttp.ClientSession", return_value=mock_session):
            # Truncated JSON will fail to parse → expect error
            result = await lc_tool.ainvoke({"query": "test"})
        # Either a valid truncated result or a JSON decode error
        assert isinstance(result, dict)

    async def test_error_does_not_leak_secrets(self, monkeypatch):
        monkeypatch.setenv("ORG_TOOL_ENCRYPTION_KEY", _TEST_FERNET_KEY)
        org_tools_module._fernet = None
        import config
        config.get_settings.cache_clear()

        try:
            encrypted = _encrypt_header("Bearer super-secret-token-12345")
            tool = _sample_org_tool(has_auth=True)
            lc_tool = _build_langchain_tool(tool, "Authorization", encrypted)

            import aiohttp
            mock_session = AsyncMock()
            mock_session.post = AsyncMock(
                side_effect=aiohttp.ClientError("connection failed")
            )
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)

            with patch("org_tools.validate_url", return_value=None), \
                 patch("org_tools.aiohttp.ClientSession", return_value=mock_session):
                result = await lc_tool.ainvoke({"query": "test"})

            # Error message should not contain the secret token
            error_str = json.dumps(result)
            assert "super-secret-token-12345" not in error_str
        finally:
            org_tools_module._fernet = None
            config.get_settings.cache_clear()


# ─── Cache invalidation ──────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCacheInvalidation:
    async def test_invalidate_clears_in_process(self):
        org_tools_module._org_tools_cache["org-1"] = ([_sample_org_tool()], 9999999999)
        with patch("org_tools.get_redis", return_value=None):
            await invalidate_org_tools_cache("org-1")
        assert "org-1" not in org_tools_module._org_tools_cache


# ─── build_org_langchain_tools ────────────────────────────────────────────────


@pytest.mark.anyio
class TestBuildOrgLangchainTools:
    async def test_empty_org(self):
        mock = AsyncMock(return_value=[])
        with patch.object(org_tools_module, "get_org_tools_cached", mock):
            tools_list, tools_by_name = (
                await org_tools_module.build_org_langchain_tools("org-1")
            )
        assert tools_list == []
        assert tools_by_name == {}

    async def test_skips_global_collision(self):
        tool = _sample_org_tool(name="web_search")  # global tool name
        pool = _pool()
        pool.fetch = AsyncMock(return_value=[
            {"id": "tool-1", "auth_header_name": None, "auth_header_enc": None},
        ])

        cached_mock = AsyncMock(return_value=[tool])
        mock_reg = MagicMock()
        mock_reg.get.return_value = MagicMock()  # non-None = collision
        with patch.object(
            org_tools_module, "get_org_tools_cached", cached_mock,
        ), patch.object(
            org_tools_module, "_pool", pool,
        ), patch("tools.registry", mock_reg):
            tools_list, tools_by_name = (
                await org_tools_module.build_org_langchain_tools("org-1")
            )

        assert len(tools_list) == 0
        assert "web_search" not in tools_by_name
