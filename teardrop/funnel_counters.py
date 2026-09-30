# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""In-process discovery-stage hit counters flushed as bounded hourly aggregates.

Public discovery surfaces (agent card, x402 discovery, catalog, MCP tools/list,
402 challenges) increment an in-memory counter instead of writing a row per
request. A background loop flushes the counters as upserts keyed by
``(surface, bucket_hour)``, so unauthenticated traffic can never drive
row-amplification and no PII (IP, user agent, referer) is ever stored.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from shared.db_pool import PgPool

logger = logging.getLogger(__name__)

# Bounded surface vocabulary — the only values ever written to the table.
SURFACE_AGENT_CARD = "agent_card"
SURFACE_X402_DISCOVERY = "x402_discovery"
SURFACE_MCP_SERVER_CARD = "mcp_server_card"
SURFACE_CATALOG = "catalog"
SURFACE_QUOTE = "quote"
SURFACE_TOOLS_LIST = "tools_list"
# Subset of tools_list without a Bearer token: the anonymous x402 audience.
SURFACE_TOOLS_LIST_ANON = "tools_list_anon"
SURFACE_MCP_402_CHALLENGE = "mcp_402_challenge"
# Subsets of mcp_402_challenge for anonymous x402 callers; credit-rail 402s count only in the total.
SURFACE_MCP_402_NO_PAYMENT = "mcp_402_no_payment"
SURFACE_MCP_402_PAYMENT_INVALID = "mcp_402_payment_invalid"
# Partition of no_payment + payment_invalid by header-derived client class.
SURFACE_MCP_402_ANON_CLIENT_PREFIX = "mcp_402_anon_client:"
# MCP `initialize` handshakes by clientInfo.name bucket (the app is stateless, so tools/call carries no clientInfo).
SURFACE_MCP_INITIALIZE_PREFIX = "mcp_initialize:"

CLIENT_CLASSES: tuple[str, ...] = ("bot", "mcp", "browser", "script", "unknown")
MCP_HOSTS: tuple[str, ...] = ("claude", "cursor", "vscode", "openai", "inspector", "x402", "other", "none")

_BOT_UA_TOKENS = ("bot", "crawl", "spider", "scan", "monitor", "uptime", "probe", "validat", "health", "preview", "headless")
_SCRIPT_UA_TOKENS = (
    "curl",
    "wget",
    "python",
    "httpx",
    "aiohttp",
    "requests",
    "node",
    "undici",
    "axios",
    "go-http",
    "okhttp",
    "java",
    "ruby",
    "reqwest",
    "deno",
    "bun/",
    "postman",
    "insomnia",
)
# First match wins: "cursor-vscode" must bucket as cursor.
_MCP_HOST_TOKENS = (
    ("claude", "claude"),
    ("cursor", "cursor"),
    ("visual studio code", "vscode"),
    ("vscode", "vscode"),
    ("openai", "openai"),
    ("chatgpt", "openai"),
    ("codex", "openai"),
    ("inspector", "inspector"),
    ("x402", "x402"),
)
_MAX_CLASSIFIED_CHARS = 256

VALID_SURFACES: frozenset[str] = frozenset(
    {
        SURFACE_AGENT_CARD,
        SURFACE_X402_DISCOVERY,
        SURFACE_MCP_SERVER_CARD,
        SURFACE_CATALOG,
        SURFACE_QUOTE,
        SURFACE_TOOLS_LIST,
        SURFACE_TOOLS_LIST_ANON,
        SURFACE_MCP_402_CHALLENGE,
        SURFACE_MCP_402_NO_PAYMENT,
        SURFACE_MCP_402_PAYMENT_INVALID,
        *(SURFACE_MCP_402_ANON_CLIENT_PREFIX + cls for cls in CLIENT_CLASSES),
        *(SURFACE_MCP_INITIALIZE_PREFIX + host for host in MCP_HOSTS),
    }
)

_pool: PgPool | None = None
_enabled: bool = False
_counters: dict[tuple[str, datetime], int] = {}


def init_funnel_counters(pool: PgPool, enabled: bool) -> None:
    """Bind the shared pool and the feature flag after migrations complete."""
    global _pool, _enabled
    _pool = pool
    _enabled = enabled


def close_funnel_counters() -> None:
    """Release the pool reference and drop any unflushed counters."""
    global _pool, _enabled
    _pool = None
    _enabled = False
    _counters.clear()


def anon_challenge_client_surface(user_agent: str | None, is_mcp: bool) -> str:
    """Bucket an anonymous x402 challenger; self-identified bots win over the MCP transport signal."""
    ua = (user_agent or "")[:_MAX_CLASSIFIED_CHARS].lower()
    if any(token in ua for token in _BOT_UA_TOKENS):
        cls = "bot"
    elif is_mcp:
        cls = "mcp"
    elif ua.startswith("mozilla/"):
        cls = "browser"
    elif any(token in ua for token in _SCRIPT_UA_TOKENS):
        cls = "script"
    else:
        cls = "unknown"
    return SURFACE_MCP_402_ANON_CLIENT_PREFIX + cls


def mcp_initialize_surface(client_name: object) -> str:
    """Bucket an MCP ``initialize`` clientInfo.name into the bounded host vocabulary."""
    if not isinstance(client_name, str) or not client_name.strip():
        return SURFACE_MCP_INITIALIZE_PREFIX + "none"
    name = client_name[:_MAX_CLASSIFIED_CHARS].lower()
    host = next((bucket for token, bucket in _MCP_HOST_TOKENS if token in name), "other")
    return SURFACE_MCP_INITIALIZE_PREFIX + host


def record_discovery_hit(surface: str) -> None:
    """Increment the in-process counter for a surface. Never raises.

    Synchronous and O(1): safe to call from request handlers on the hot path.
    Unknown surfaces are ignored (bounded vocabulary enforced here, not in SQL).
    """
    if not _enabled or surface not in VALID_SURFACES:
        return
    bucket = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    _counters[(surface, bucket)] = _counters.get((surface, bucket), 0) + 1


async def flush_discovery_counters() -> int:
    """Upsert pending counters into ``discovery_stage_counts``.

    Returns the number of distinct (surface, hour) buckets flushed. Failures
    are logged and the counters are retained so the next flush retries them.
    """
    if _pool is None or not _counters:
        return 0

    pending = dict(_counters)
    try:
        await _pool.executemany(
            """
            INSERT INTO discovery_stage_counts (surface, bucket_hour, count)
            VALUES ($1, $2, $3)
            ON CONFLICT (surface, bucket_hour)
            DO UPDATE SET count = discovery_stage_counts.count + EXCLUDED.count
            """,
            [(surface, bucket, count) for (surface, bucket), count in sorted(pending.items())],
        )
    except Exception:
        logger.warning("Discovery counter flush failed; %d bucket(s) retained for retry", len(pending), exc_info=True)
        return 0

    for key in pending:
        _counters.pop(key, None)
    return len(pending)
