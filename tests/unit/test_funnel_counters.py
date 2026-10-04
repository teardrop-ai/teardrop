# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for the in-process discovery-stage hit counters."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import teardrop.funnel_counters as funnel_module
from teardrop.funnel_counters import (
    CLIENT_CLASSES,
    MCP_HOSTS,
    SURFACE_AGENT_CARD,
    SURFACE_CATALOG,
    SURFACE_MCP_402_CHALLENGE,
    VALID_SURFACES,
    anon_challenge_client_surface,
    close_funnel_counters,
    flush_discovery_counters,
    init_funnel_counters,
    mcp_initialize_surface,
    mcp_meta_client_name,
    record_discovery_hit,
)


def _pool():
    pool = MagicMock()
    pool.executemany = AsyncMock(return_value=None)
    return pool


@pytest.fixture(autouse=True)
def _reset_counters():
    close_funnel_counters()
    yield
    close_funnel_counters()


class TestRecordDiscoveryHit:
    def test_disabled_counter_is_noop(self):
        record_discovery_hit(SURFACE_AGENT_CARD)
        assert funnel_module._counters == {}

    def test_enabled_counter_increments(self):
        init_funnel_counters(_pool(), enabled=True)
        record_discovery_hit(SURFACE_AGENT_CARD)
        record_discovery_hit(SURFACE_AGENT_CARD)
        record_discovery_hit(SURFACE_CATALOG)

        assert len(funnel_module._counters) == 2
        assert sum(funnel_module._counters.values()) == 3

    def test_unknown_surface_is_ignored(self):
        init_funnel_counters(_pool(), enabled=True)
        record_discovery_hit("not_a_surface")
        record_discovery_hit("")
        record_discovery_hit("mcp_402_anon_client:curl/8.0")

        assert funnel_module._counters == {}


class TestClientClassification:
    @pytest.mark.parametrize(
        ("user_agent", "is_mcp", "expected"),
        [
            ("x402scan-indexer/1.0", True, "bot"),
            ("Mozilla/5.0 (compatible; Googlebot/2.1)", False, "bot"),
            ("python-httpx/0.28.1", True, "mcp"),
            ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130", False, "browser"),
            ("curl/8.9.1", False, "script"),
            ("node", False, "script"),
            ("", False, "unknown"),
            (None, False, "unknown"),
            ("SomeAgent/1.0", False, "unknown"),
        ],
    )
    def test_anon_challenge_client_surface(self, user_agent, is_mcp, expected):
        surface = anon_challenge_client_surface(user_agent, is_mcp)

        assert surface == f"mcp_402_anon_client:{expected}"
        assert surface in VALID_SURFACES

    @pytest.mark.parametrize(
        ("client_name", "expected"),
        [
            # Pre-existing buckets must not shift as the vocabulary widens.
            ("claude-ai", "claude"),
            ("claude-code", "claude"),
            ("cursor-vscode", "cursor"),
            ("Visual Studio Code", "vscode"),
            ("openai-mcp", "openai"),
            ("mcp-inspector", "inspector"),
            ("x402-mcp-client", "x402"),
            # First match wins by token-list order, not position in the name:
            # "cursor" precedes "vscode", and "openai" precedes "chatgpt".
            ("vscode-cursor", "cursor"),
            ("chatgpt-openai", "openai"),
            ("cline-cursor", "cursor"),
            ("anthropicclaude-cursor", "claude"),
            ("x402client", "x402"),
            ("x402client-cline", "x402"),
            # Agent-builder apps.
            ("cline", "cline"),
            ("Roo Code", "roo"),
            ("roo-code", "roo"),
            ("windsurf", "windsurf"),
            ("zed", "zed"),
            ("Continue", "continue"),
            ("block-goose", "goose"),
            ("Cherry Studio", "cherry"),
            ("LibreChat", "librechat"),
            ("n8n-mcp", "n8n"),
            ("dify-agent", "dify"),
            ("open-webui", "openwebui"),
            ("OpenWebUI", "openwebui"),
            ("kiro", "kiro"),
            ("augment", "augment"),
            ("github-copilot", "copilot"),
            ("qwen-code", "qwen"),
            ("gemini-cli", "gemini"),
            # Programmatic clients collapse into one shared bucket.
            ("fastmcp", "sdk"),
            ("@modelcontextprotocol/sdk", "sdk"),
            ("langchain-mcp-adapter", "sdk"),
            ("langgraph", "sdk"),
            ("crewai-agents", "sdk"),
            ("smol", "sdk"),
            ("agno", "sdk"),
            ("mcp-python-sdk", "sdk"),
            ("mcp-node", "sdk"),
            ("autogen", "sdk"),
            # Legacy substring behavior remains; additional aliases need boundaries.
            ("anthropicclaude", "claude"),
            ("augmented-code", "other"),
            ("discontinue", "other"),
            ("mongoose", "other"),
            ("room", "other"),
            ("kangaroo", "other"),
            ("organized", "other"),
            ("difyy", "other"),
            ("continuous-improver", "other"),
            ("my-agent", "other"),
            ("   ", "none"),
            (None, "none"),
            ({"name": "claude"}, "none"),
            (123, "none"),
            (" " * 256 + "cline", "other"),
            (" " * 255 + "cline", "other"),
            (" " * 251 + "cline", "cline"),
            (" " * 256 + "claude", "other"),
        ],
    )
    def test_mcp_initialize_surface(self, client_name, expected):
        surface = mcp_initialize_surface(client_name)

        assert surface == f"mcp_initialize:{expected}"
        assert surface in VALID_SURFACES

    @pytest.mark.parametrize(("token", "expected"), funnel_module._MCP_ADDITIONAL_HOST_TOKENS)
    @pytest.mark.parametrize(("prefix", "suffix"), [("", ""), ("client/", "/1.0"), ("client_", "_1.0")])
    def test_additional_host_alias_boundaries(self, token, expected, prefix, suffix):
        surface = mcp_initialize_surface(f"{prefix}{token.upper()}{suffix}")

        assert surface == f"mcp_initialize:{expected}"
        assert surface in VALID_SURFACES

    @pytest.mark.parametrize(("token", "expected"), funnel_module._MCP_ADDITIONAL_HOST_TOKENS)
    def test_additional_host_alias_rejects_embedded_words(self, token, expected):
        assert mcp_initialize_surface(f"prefix{token}suffix") == "mcp_initialize:other"

    @pytest.mark.parametrize(("token", "expected"), funnel_module._MCP_HOST_TOKENS)
    def test_legacy_host_substrings_take_precedence(self, token, expected):
        assert mcp_initialize_surface(f"prefix{token.upper()}suffix-cline") == f"mcp_initialize:{expected}"

    def test_vocabulary_is_bounded(self):
        assert len(VALID_SURFACES) == 12 + len(CLIENT_CLASSES) + 2 * len(MCP_HOSTS)
        assert len(MCP_HOSTS) == 25
        assert len(set(MCP_HOSTS)) == len(MCP_HOSTS)

    @pytest.mark.parametrize(("client_name", "expected"), [("claude-ai", "claude"), ("my-agent", "other"), ("", "none")])
    def test_mcp_call_client_surface_shares_host_buckets(self, client_name, expected):
        surface = funnel_module.mcp_call_client_surface(client_name)

        assert surface == f"mcp_call_client:{expected}"
        assert surface in VALID_SURFACES

    def test_host_buckets_are_split_part_safe(self):
        """Panel 4 splits surfaces on ':'; a bucket containing one would be truncated."""
        assert all(":" not in host and host for host in MCP_HOSTS)

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({"_meta": {"io.modelcontextprotocol/clientInfo": {"name": "claude-ai", "version": "1"}}}, "claude-ai"),
            ({"_meta": {"io.modelcontextprotocol/clientInfo": {"name": ""}}}, ""),
            ({"_meta": {"io.modelcontextprotocol/clientInfo": {"name": 3}}}, None),
            ({"_meta": {"io.modelcontextprotocol/clientInfo": {}}}, None),
            ({"_meta": {"io.modelcontextprotocol/clientInfo": "claude-ai"}}, None),
            ({"_meta": {}}, None),
            ({"_meta": None}, None),
            ({}, None),
            (None, None),
            ("_meta", None),
        ],
    )
    def test_mcp_meta_client_name(self, params, expected):
        assert mcp_meta_client_name(params) == expected


@pytest.mark.anyio
class TestFlushDiscoveryCounters:
    async def test_flush_upserts_and_clears_counters(self):
        pool = _pool()
        init_funnel_counters(pool, enabled=True)
        record_discovery_hit(SURFACE_AGENT_CARD)
        record_discovery_hit(SURFACE_MCP_402_CHALLENGE)

        flushed = await flush_discovery_counters()

        assert flushed == 2
        assert funnel_module._counters == {}
        sql = pool.executemany.await_args.args[0]
        assert "INSERT INTO discovery_stage_counts" in sql
        assert "ON CONFLICT (surface, bucket_hour)" in sql
        rows = pool.executemany.await_args.args[1]
        assert len(rows) == 2

    async def test_flush_without_pool_or_counters_is_noop(self):
        init_funnel_counters(_pool(), enabled=True)
        assert await flush_discovery_counters() == 0

        close_funnel_counters()
        record_discovery_hit(SURFACE_AGENT_CARD)
        assert await flush_discovery_counters() == 0

    async def test_flush_failure_retains_counters_for_retry(self):
        pool = _pool()
        pool.executemany = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        init_funnel_counters(pool, enabled=True)
        record_discovery_hit(SURFACE_AGENT_CARD)

        assert await flush_discovery_counters() == 0
        assert len(funnel_module._counters) == 1

    async def test_repeated_hits_same_hour_aggregate_into_one_bucket(self):
        pool = _pool()
        init_funnel_counters(pool, enabled=True)
        for _ in range(50):
            record_discovery_hit(SURFACE_AGENT_CARD)

        flushed = await flush_discovery_counters()

        assert flushed == 1
        rows = pool.executemany.await_args.args[1]
        assert len(rows) == 1
        assert rows[0][2] == 50
