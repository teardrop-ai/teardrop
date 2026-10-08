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
    discover_client_surface,
    flush_discovery_counters,
    init_funnel_counters,
    mcp_initialize_surface,
    mcp_meta_client_name,
    record_discover_hit,
    record_discovery_hit,
    tools_list_anon_client_surface,
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
        ("user_agent", "expected"),
        [
            ("x402scan-indexer/1.0", "bot"),
            ("Mozilla/5.0 (Macintosh) Safari/605", "browser"),
            ("python-httpx/0.28.1", "script"),
            (None, "unknown"),
        ],
    )
    def test_discover_client_surface_has_no_mcp_class(self, user_agent, expected):
        surface = discover_client_surface(user_agent)

        assert surface == f"discover_client:{expected}"
        assert surface in VALID_SURFACES

    @pytest.mark.parametrize(
        ("user_agent", "is_mcp", "expected"),
        [
            ("smithery-validator/1.0", True, "bot"),
            ("node", True, "mcp"),
            ("node", False, "script"),
        ],
    )
    def test_tools_list_anon_client_surface(self, user_agent, is_mcp, expected):
        surface = tools_list_anon_client_surface(user_agent, is_mcp)

        assert surface == f"tools_list_anon_client:{expected}"
        assert surface in VALID_SURFACES

    def test_record_discover_hit_records_stage_and_partition(self):
        init_funnel_counters(_pool(), enabled=True)
        record_discover_hit(SURFACE_AGENT_CARD, "Googlebot/2.1")

        assert sorted(surface for surface, _ in funnel_module._counters) == ["agent_card", "discover_client:bot"]

    def test_every_client_class_is_a_valid_partition(self):
        prefixes = ("mcp_402_anon_client:", "discover_client:", "tools_list_anon_client:", "tools_call_free_anon_client:")
        for prefix in prefixes:
            assert {prefix + cls for cls in CLIENT_CLASSES} <= VALID_SURFACES

    def test_tools_call_free_anon_client_surface(self):
        assert funnel_module.tools_call_free_anon_client_surface("node", True) == "tools_call_free_anon_client:mcp"

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
        source_surfaces = 4 * len(funnel_module.SOURCE_TAGS)
        assert len(VALID_SURFACES) == 13 + 4 * len(CLIENT_CLASSES) + 2 * len(MCP_HOSTS) + source_surfaces
        assert len(MCP_HOSTS) == 26
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


class TestUnclassifiedClientLogging:
    _MSG = "mcp unclassified client"

    def _lines(self, caplog):
        return [r.getMessage() for r in caplog.records if r.getMessage().startswith(self._MSG)]

    def test_logs_each_unclassified_name_once_per_hour(self, caplog):
        init_funnel_counters(_pool(), enabled=True)
        with caplog.at_level("INFO", logger=funnel_module.__name__):
            for _ in range(3):
                mcp_initialize_surface("Acme-Agent/2.1")
            mcp_initialize_surface("claude-ai")
            mcp_initialize_surface("")

        assert self._lines(caplog) == ["mcp unclassified client name='acme-agent/2.1'"]

    def test_sanitizes_and_truncates(self, caplog):
        init_funnel_counters(_pool(), enabled=True)
        with caplog.at_level("INFO", logger=funnel_module.__name__):
            mcp_initialize_surface("evil\nname\x1b[31m" + "a" * 100)

        (line,) = self._lines(caplog)
        assert "\n" not in line and "\x1b" not in line
        assert line == "mcp unclassified client name=" + repr(("evil?name??31m" + "a" * 100)[:64])

    def test_caps_distinct_names_per_hour(self, caplog):
        init_funnel_counters(_pool(), enabled=True)
        cap = funnel_module._UNCLASSIFIED_LOG_MAX_PER_HOUR
        with caplog.at_level("INFO", logger=funnel_module.__name__):
            for i in range(cap + 5):
                mcp_initialize_surface(f"agent-{i}")

        assert len(self._lines(caplog)) == cap

    def test_disabled_counters_do_not_log(self, caplog):
        with caplog.at_level("INFO", logger=funnel_module.__name__):
            mcp_initialize_surface("acme-agent")

        assert self._lines(caplog) == []

    def test_bot_bucket_is_not_logged(self, caplog):
        init_funnel_counters(_pool(), enabled=True)
        with caplog.at_level("INFO", logger=funnel_module.__name__):
            mcp_initialize_surface("smithery-probe")

        assert self._lines(caplog) == []


class TestIndexerClassification:
    @pytest.mark.parametrize(
        "client_name",
        [
            # Every clientInfo.name in the 2026-10-07 unclassified log sample.
            "smithery-probe",
            "brickbluebot",
            "glama",
            "cdp-bazaar-discovery",
            "agentprobe",
            "taifoon-harvester",
            "agentalog-sonda",
            "mcp-rugpull-research",
            "agent-tools.cloud",
            "UptimeMonitor/1.0",
        ],
    )
    def test_indexer_names_bucket_as_bot(self, client_name):
        assert mcp_initialize_surface(client_name) == "mcp_initialize:bot"
        assert funnel_module.mcp_call_client_surface(client_name) == "mcp_call_client:bot"

    @pytest.mark.parametrize(
        ("client_name", "expected"),
        [("claude-code-preview", "claude"), ("cursor-health", "cursor"), ("fastmcp-scanner", "sdk")],
    )
    def test_known_hosts_win_over_bot_tokens(self, client_name, expected):
        assert mcp_initialize_surface(client_name) == f"mcp_initialize:{expected}"

    def test_research_agents_are_not_indexers(self):
        assert mcp_initialize_surface("gpt-researcher") == "mcp_initialize:other"

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, "mcp"),
            ({"client_name": "smithery-probe"}, "bot"),
            ({"client_name": "claude-ai"}, "mcp"),
            ({"client_name": "my-agent"}, "mcp"),
            ({"flagged": True}, "bot"),
        ],
    )
    def test_client_name_and_flag_drive_bot_class(self, kwargs, expected):
        for helper, prefix in (
            (anon_challenge_client_surface, "mcp_402_anon_client:"),
            (tools_list_anon_client_surface, "tools_list_anon_client:"),
            (funnel_module.tools_call_free_anon_client_surface, "tools_call_free_anon_client:"),
        ):
            assert helper("node", True, **kwargs) == prefix + expected

    def test_seed_script_user_agent_is_bot(self):
        assert anon_challenge_client_surface("teardrop-seed-bot/1.0", False) == "mcp_402_anon_client:bot"


class TestListingSource:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("smithery", "smithery"),
            (" Glama ", "glama"),
            ("mcp-so", "mcp-so"),
            ("some-new-directory", "other"),
            ("x" * 500, "other"),
            ("", None),
            ("   ", None),
            (None, None),
            (["smithery"], None),
        ],
    )
    def test_listing_source_is_bounded(self, raw, expected):
        assert funnel_module.listing_source(raw) == expected

    def test_every_source_partition_is_valid_and_split_part_safe(self):
        for prefix in funnel_module._SOURCE_PREFIXES:
            assert prefix.count(":") == 1 and prefix.endswith(":")
            assert {prefix + tag for tag in funnel_module.SOURCE_TAGS} <= VALID_SURFACES
        assert all(":" not in tag for tag in funnel_module.SOURCE_TAGS)

    @pytest.mark.parametrize(
        ("raw", "client_surface", "expected"),
        [
            ("smithery", "mcp_402_anon_client:mcp", ["mcp_402_anon_src:smithery"]),
            ("smithery", None, ["mcp_402_anon_src:smithery"]),
            ("weird", "mcp_402_anon_client:script", ["mcp_402_anon_src:other"]),
            ("smithery", "mcp_402_anon_client:bot", []),
            (None, "mcp_402_anon_client:mcp", []),
        ],
    )
    def test_record_source_hit_skips_bots_and_untagged(self, raw, client_surface, expected):
        init_funnel_counters(_pool(), enabled=True)

        funnel_module.record_source_hit(funnel_module.SURFACE_MCP_402_ANON_SRC_PREFIX, raw, client_surface)

        assert [surface for surface, _ in funnel_module._counters] == expected


@pytest.mark.anyio
class TestIndexerIpFlag:
    @pytest.fixture(autouse=True)
    def _no_redis(self, monkeypatch):
        monkeypatch.setattr("teardrop.cache.get_redis", lambda: None)

    async def test_flag_roundtrip_in_process(self):
        init_funnel_counters(_pool(), enabled=True)
        assert await funnel_module.is_indexer_ip("203.0.113.7") is False

        await funnel_module.mark_indexer_ip("203.0.113.7")

        assert await funnel_module.is_indexer_ip("203.0.113.7") is True
        assert await funnel_module.is_indexer_ip("203.0.113.8") is False
        assert all("203.0.113.7" not in key for key in funnel_module._indexer_ips)

    async def test_flag_expires(self, monkeypatch):
        init_funnel_counters(_pool(), enabled=True)
        await funnel_module.mark_indexer_ip("203.0.113.7")
        now = funnel_module.time.monotonic()
        monkeypatch.setattr(funnel_module.time, "monotonic", lambda: now + funnel_module._INDEXER_IP_TTL_SECONDS + 1)

        assert await funnel_module.is_indexer_ip("203.0.113.7") is False
        assert funnel_module._indexer_ips == {}

    async def test_disabled_or_missing_ip_is_noop(self):
        await funnel_module.mark_indexer_ip("203.0.113.7")
        assert funnel_module._indexer_ips == {}
        init_funnel_counters(_pool(), enabled=True)
        await funnel_module.mark_indexer_ip(None)
        assert await funnel_module.is_indexer_ip(None) is False
        assert funnel_module._indexer_ips == {}

    async def test_fallback_is_bounded(self, monkeypatch):
        init_funnel_counters(_pool(), enabled=True)
        monkeypatch.setattr(funnel_module, "_INDEXER_IP_MAX_KEYS", 3)
        for i in range(5):
            await funnel_module.mark_indexer_ip(f"203.0.113.{i}")

        assert len(funnel_module._indexer_ips) == 3
        assert await funnel_module.is_indexer_ip("203.0.113.0") is False
        assert await funnel_module.is_indexer_ip("203.0.113.4") is True

    async def test_redis_used_when_available(self, monkeypatch):
        redis = MagicMock()
        redis.set = AsyncMock()
        redis.exists = AsyncMock(return_value=1)
        monkeypatch.setattr("teardrop.cache.get_redis", lambda: redis)
        init_funnel_counters(_pool(), enabled=True)

        await funnel_module.mark_indexer_ip("203.0.113.7")
        assert await funnel_module.is_indexer_ip("203.0.113.7") is True

        key = redis.set.await_args.args[0]
        assert key.startswith("teardrop:funnelbot:") and "203.0.113.7" not in key
        assert redis.set.await_args.kwargs == {"ex": funnel_module._INDEXER_IP_TTL_SECONDS}
        assert funnel_module._indexer_ips == {}

    async def test_redis_failure_falls_back(self, monkeypatch):
        redis = MagicMock()
        redis.set = AsyncMock(side_effect=RuntimeError("down"))
        redis.exists = AsyncMock(side_effect=RuntimeError("down"))
        monkeypatch.setattr("teardrop.cache.get_redis", lambda: redis)
        init_funnel_counters(_pool(), enabled=True)

        await funnel_module.mark_indexer_ip("203.0.113.7")

        assert await funnel_module.is_indexer_ip("203.0.113.7") is True


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
