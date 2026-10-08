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

import hashlib
import logging
import re
import time
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
# Partition of the discover stage (agent_card + x402_discovery + mcp_server_card + catalog) by client class.
SURFACE_DISCOVER_CLIENT_PREFIX = "discover_client:"
# Partition of tools_list_anon by client class.
SURFACE_TOOLS_LIST_ANON_CLIENT_PREFIX = "tools_list_anon_client:"
# Anonymous tools/call admitted to an allowlisted zero-cost tool (activation before any payment).
SURFACE_TOOLS_CALL_FREE_ANON = "tools_call_free_anon"
SURFACE_TOOLS_CALL_FREE_ANON_CLIENT_PREFIX = "tools_call_free_anon_client:"
# MCP `initialize` (legacy) / `server/discover` (2026-07-28) handshakes by clientInfo.name bucket.
SURFACE_MCP_INITIALIZE_PREFIX = "mcp_initialize:"
# 2026-07-28 tools/call by params._meta clientInfo bucket; legacy calls carry no clientInfo and are not counted.
SURFACE_MCP_CALL_CLIENT_PREFIX = "mcp_call_client:"
# Every JSON-RPC request with a method — the denominator for the modern-envelope share.
SURFACE_MCP_REQUEST = "mcp_request"
# Requests carrying the 2026-07-28 per-request envelope (params._meta protocol version key).
SURFACE_MCP_MODERN_ENVELOPE = "mcp_modern_envelope"
# Listing-level attribution: the `utm_source` tag on the MCP URL we submit to each directory. Query
# strings carry no MCP or payment meaning, so clients send them unchanged. The x402 Bazaar strips the
# query from resource URLs, so Bazaar traffic is the untagged remainder.
SOURCE_QUERY_PARAM = "utm_source"
SOURCE_TAGS: tuple[str, ...] = ("smithery", "glama", "pulsemcp", "mcp-so", "agent-tools", "github", "docs", "other")
SURFACE_TOOLS_LIST_ANON_SRC_PREFIX = "tools_list_anon_src:"
SURFACE_MCP_402_ANON_SRC_PREFIX = "mcp_402_anon_src:"
SURFACE_TOOLS_CALL_FREE_ANON_SRC_PREFIX = "tools_call_free_anon_src:"
SURFACE_X402_SETTLED_SRC_PREFIX = "x402_settled_src:"
_SOURCE_PREFIXES = (
    SURFACE_TOOLS_LIST_ANON_SRC_PREFIX,
    SURFACE_MCP_402_ANON_SRC_PREFIX,
    SURFACE_TOOLS_CALL_FREE_ANON_SRC_PREFIX,
    SURFACE_X402_SETTLED_SRC_PREFIX,
)

# Reserved `_meta` keys of the 2026-07-28 stateless envelope (spec-reserved prefix).
MCP_PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
MCP_CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"

CLIENT_CLASSES: tuple[str, ...] = ("bot", "mcp", "browser", "script", "unknown")
# Fixed buckets bound telemetry cardinality; `sdk` groups frameworks for readability.
MCP_HOSTS: tuple[str, ...] = (
    "claude",
    "cursor",
    "vscode",
    "openai",
    "inspector",
    "x402",
    "cline",
    "roo",
    "windsurf",
    "zed",
    "continue",
    "goose",
    "cherry",
    "librechat",
    "n8n",
    "dify",
    "openwebui",
    "kiro",
    "augment",
    "copilot",
    "qwen",
    "gemini",
    "sdk",
    "bot",
    "other",
    "none",
)

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
# Preserve legacy ordered substring matching before trying additional aliases.
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
_MCP_ADDITIONAL_HOST_TOKENS = (
    ("cline", "cline"),
    ("roo", "roo"),
    ("windsurf", "windsurf"),
    ("zed", "zed"),
    ("continue", "continue"),
    ("goose", "goose"),
    ("cherry", "cherry"),
    ("librechat", "librechat"),
    ("n8n", "n8n"),
    ("dify", "dify"),
    ("open-webui", "openwebui"),
    ("openwebui", "openwebui"),
    ("kiro", "kiro"),
    ("augment", "augment"),
    ("copilot", "copilot"),
    ("qwen", "qwen"),
    ("gemini", "gemini"),
    ("fastmcp", "sdk"),
    ("modelcontextprotocol", "sdk"),
    ("langchain", "sdk"),
    ("langgraph", "sdk"),
    ("crewai", "sdk"),
    ("smol", "sdk"),
    ("autogen", "sdk"),
    ("agno", "sdk"),
    ("mcp-python", "sdk"),
    ("mcp-node", "sdk"),
)
_MCP_ADDITIONAL_HOST_PATTERNS = tuple(
    (re.compile(r"(?:^|[^a-z0-9])" + re.escape(token) + r"(?:$|[^a-z0-9])"), bucket)
    for token, bucket in _MCP_ADDITIONAL_HOST_TOKENS
)
_MAX_CLASSIFIED_CHARS = 256
# Registries and scanners seen in clientInfo.name without a generic bot token (2026-10-07 log sample).
_INDEXER_NAME_TOKENS = ("smithery", "glama", "bazaar", "harvest", "sonda", "rugpull", "agent-tools")
# A legacy tools/call carries no clientInfo and the server is stateless, so an indexer's handshake
# flags its IP for this long; later anonymous calls from that IP count as `bot`.
_INDEXER_IP_TTL_SECONDS = 900
_INDEXER_IP_MAX_KEYS = 10_000
# Log-only sampling of unclassified clientInfo.name values so `other` can be named; never persisted.
_UNCLASSIFIED_LOG_MAX_CHARS = 64
_UNCLASSIFIED_LOG_MAX_PER_HOUR = 20
_UNCLASSIFIED_NAME_UNSAFE = re.compile(r"[^a-z0-9._@/ -]")

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
        SURFACE_MCP_MODERN_ENVELOPE,
        SURFACE_MCP_REQUEST,
        SURFACE_TOOLS_CALL_FREE_ANON,
        *(SURFACE_MCP_402_ANON_CLIENT_PREFIX + cls for cls in CLIENT_CLASSES),
        *(SURFACE_DISCOVER_CLIENT_PREFIX + cls for cls in CLIENT_CLASSES),
        *(SURFACE_TOOLS_LIST_ANON_CLIENT_PREFIX + cls for cls in CLIENT_CLASSES),
        *(SURFACE_TOOLS_CALL_FREE_ANON_CLIENT_PREFIX + cls for cls in CLIENT_CLASSES),
        *(SURFACE_MCP_INITIALIZE_PREFIX + host for host in MCP_HOSTS),
        *(SURFACE_MCP_CALL_CLIENT_PREFIX + host for host in MCP_HOSTS),
        *(prefix + tag for prefix in _SOURCE_PREFIXES for tag in SOURCE_TAGS),
    }
)

_pool: PgPool | None = None
_enabled: bool = False
_counters: dict[tuple[str, datetime], int] = {}
_unclassified_logged: tuple[datetime | None, set[str]] = (None, set())
_indexer_ips: dict[str, float] = {}


def init_funnel_counters(pool: PgPool, enabled: bool) -> None:
    """Bind the shared pool and the feature flag after migrations complete."""
    global _pool, _enabled
    _pool = pool
    _enabled = enabled


def close_funnel_counters() -> None:
    """Release the pool reference and drop any unflushed counters."""
    global _pool, _enabled, _unclassified_logged
    _pool = None
    _enabled = False
    _counters.clear()
    _unclassified_logged = (None, set())
    _indexer_ips.clear()


def _client_class(user_agent: str | None, is_mcp: bool, *, client_name: object = None, flagged: bool = False) -> str:
    """Client class from headers, clientInfo.name, and the indexer-IP flag; bots win over the MCP signal."""
    ua = (user_agent or "")[:_MAX_CLASSIFIED_CHARS].lower()
    if flagged or any(token in ua for token in _BOT_UA_TOKENS):
        return "bot"
    if client_name is not None and _mcp_host_bucket(client_name) == "bot":
        return "bot"
    if is_mcp:
        return "mcp"
    if ua.startswith("mozilla/"):
        return "browser"
    if any(token in ua for token in _SCRIPT_UA_TOKENS):
        return "script"
    return "unknown"


def anon_challenge_client_surface(
    user_agent: str | None, is_mcp: bool, *, client_name: object = None, flagged: bool = False
) -> str:
    """Bucket an anonymous x402 challenger."""
    return SURFACE_MCP_402_ANON_CLIENT_PREFIX + _client_class(user_agent, is_mcp, client_name=client_name, flagged=flagged)


def discover_client_surface(user_agent: str | None) -> str:
    """Bucket a discover-stage hit; discovery documents are plain GETs, so there is no MCP signal."""
    return SURFACE_DISCOVER_CLIENT_PREFIX + _client_class(user_agent, False)


def tools_list_anon_client_surface(
    user_agent: str | None, is_mcp: bool, *, client_name: object = None, flagged: bool = False
) -> str:
    """Bucket an anonymous ``tools/list`` caller."""
    return SURFACE_TOOLS_LIST_ANON_CLIENT_PREFIX + _client_class(user_agent, is_mcp, client_name=client_name, flagged=flagged)


def tools_call_free_anon_client_surface(
    user_agent: str | None, is_mcp: bool, *, client_name: object = None, flagged: bool = False
) -> str:
    """Bucket an anonymous caller admitted to a free tool."""
    return SURFACE_TOOLS_CALL_FREE_ANON_CLIENT_PREFIX + _client_class(
        user_agent, is_mcp, client_name=client_name, flagged=flagged
    )


def _indexer_key(ip: str) -> str:
    return "teardrop:funnelbot:" + hashlib.sha256(ip.encode()).hexdigest()[:32]


def listing_source(raw: object) -> str | None:
    """Bounded listing source for a ``utm_source`` value; unknown tags collapse to ``other``, absent to None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    tag = raw.strip().lower()
    return tag if tag in SOURCE_TAGS else "other"


def record_source_hit(prefix: str, raw_source: object, client_surface: str | None = None) -> None:
    """Record a listing-source partition. Skips bot callers, since directories probe the tagged URL they list."""
    source = listing_source(raw_source)
    if source is None or (client_surface is not None and client_surface.endswith(":bot")):
        return
    record_discovery_hit(prefix + source)


async def mark_indexer_ip(ip: str | None) -> None:
    """Flag an IP whose handshake named a bot or indexer. Never raises."""
    if not _enabled or not ip:
        return
    key = _indexer_key(ip)
    from teardrop.cache import get_redis

    if (redis := get_redis()) is not None:
        try:
            await redis.set(key, "1", ex=_INDEXER_IP_TTL_SECONDS)
            return
        except Exception:
            logger.debug("Indexer IP flag not stored in Redis; using in-process fallback", exc_info=True)
    _indexer_ips.pop(key, None)
    _indexer_ips[key] = time.monotonic() + _INDEXER_IP_TTL_SECONDS
    if len(_indexer_ips) > _INDEXER_IP_MAX_KEYS:
        del _indexer_ips[next(iter(_indexer_ips))]


async def is_indexer_ip(ip: str | None) -> bool:
    """Whether an IP was flagged by a bot or indexer handshake within the TTL. Never raises."""
    if not _enabled or not ip:
        return False
    key = _indexer_key(ip)
    from teardrop.cache import get_redis

    if (redis := get_redis()) is not None:
        try:
            return bool(await redis.exists(key))
        except Exception:
            logger.debug("Indexer IP flag lookup failed in Redis; using in-process fallback", exc_info=True)
    expires_at = _indexer_ips.get(key)
    if expires_at is None:
        return False
    if expires_at <= time.monotonic():
        _indexer_ips.pop(key, None)
        return False
    return True


def record_discover_hit(surface: str, user_agent: str | None) -> None:
    """Record a discover-stage hit and its client-class partition."""
    record_discovery_hit(surface)
    record_discovery_hit(discover_client_surface(user_agent))


def _mcp_host_bucket(client_name: object) -> str:
    """Bucket a clientInfo.name into the bounded host vocabulary.

    Legacy hosts retain ordered substring matching. Additional aliases match
    whole segments only, so "room" and "mongoose" remain unclassified. Bot and
    indexer tokens are checked after every known host, so a real host is never hidden.
    """
    if not isinstance(client_name, str) or not client_name.strip():
        return "none"
    name = client_name[:_MAX_CLASSIFIED_CHARS].lower()
    for token, bucket in _MCP_HOST_TOKENS:
        if token in name:
            return bucket
    for pattern, bucket in _MCP_ADDITIONAL_HOST_PATTERNS:
        if pattern.search(name):
            return bucket
    if any(token in name for token in _BOT_UA_TOKENS) or any(token in name for token in _INDEXER_NAME_TOKENS):
        return "bot"
    _log_unclassified_client(name)
    return "other"


def _log_unclassified_client(name: str) -> None:
    """Log each distinct unclassified client name once per UTC hour, capped per hour."""
    global _unclassified_logged
    if not _enabled:
        return
    safe = _UNCLASSIFIED_NAME_UNSAFE.sub("?", name[:_UNCLASSIFIED_LOG_MAX_CHARS]).strip()
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    logged_hour, seen = _unclassified_logged
    if logged_hour != hour:
        seen = set()
        _unclassified_logged = (hour, seen)
    if safe in seen or len(seen) >= _UNCLASSIFIED_LOG_MAX_PER_HOUR:
        return
    seen.add(safe)
    logger.info("mcp unclassified client name=%r", safe)


def mcp_initialize_surface(client_name: object) -> str:
    """Handshake surface (``initialize`` / ``server/discover``) for a clientInfo.name."""
    return SURFACE_MCP_INITIALIZE_PREFIX + _mcp_host_bucket(client_name)


def mcp_call_client_surface(client_name: object) -> str:
    """Per-call surface for a 2026-07-28 ``tools/call`` clientInfo.name."""
    return SURFACE_MCP_CALL_CLIENT_PREFIX + _mcp_host_bucket(client_name)


def mcp_meta_client_name(params: object) -> str | None:
    """Read ``clientInfo.name`` from a 2026-07-28 request's ``params._meta`` envelope.

    Modern clients skip ``initialize``; their identity rides every request's
    ``_meta`` instead. Returns None when the envelope or name is absent.
    """
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    client_info = meta.get(MCP_CLIENT_INFO_META_KEY) if isinstance(meta, dict) else None
    name = client_info.get("name") if isinstance(client_info, dict) else None
    return name if isinstance(name, str) else None


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
