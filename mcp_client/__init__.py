# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-org MCP client connections — CRUD, session pool, tool discovery, and caching.

Allows organisations to register external MCP servers whose tools are
dynamically discovered, wrapped as LangChain StructuredTool objects, and
injected into the agent at run time alongside global and org webhook tools.

Transport: Streamable HTTP only (no stdio). Teardrop is a multi-tenant
server — spawning subprocesses per request is not viable.

This module is a thin backward-compatibility facade. The implementation lives in
focused submodules:

* :mod:`mcp_client.base`     — model, constants, pool, encryption, audit, row mapping
* :mod:`mcp_client.crud`     — create/get/list/update/delete server configs
* :mod:`mcp_client.cache`    — per-org server-list TTL caching + invalidation
* :mod:`mcp_client.session`  — SSRF-pinned Streamable-HTTP session pool
* :mod:`mcp_client.runtime`  — tool discovery, LangChain wrapping, tool builder
"""

from __future__ import annotations

from mcp_client.base import (  # noqa: F401  (re-exported for backward compatibility)
    _MAX_RESPONSE_BYTES,
    _MCP_EVENT_INSERT_SQL,
    _NAME_SEPARATOR,
    _POOL_SCOPE,
    OrgMcpServer,
    _decrypt_token,
    _encrypt_token,
    _get_pool,
    _pool,
    _record_event,
    _row_to_model,
    close_mcp_client_db,
    init_mcp_client_db,
    logger,
)
from mcp_client.cache import (  # noqa: F401  (re-exported for backward compatibility)
    _get_server_cache,
    _get_servers_cached,
    _server_caches,
    invalidate_mcp_cache,
)
from mcp_client.crud import (  # noqa: F401  (re-exported for backward compatibility)
    create_org_mcp_server,
    delete_org_mcp_server,
    get_org_mcp_server,
    list_org_mcp_servers,
    update_org_mcp_server,
)
from mcp_client.runtime import (  # noqa: F401  (re-exported for backward compatibility)
    _build_pydantic_model,
    _get_tools_lock,
    _tools_cache,
    _tools_lock,
    _wrap_mcp_tool,
    build_mcp_langchain_tools,
    discover_mcp_tools,
)
from mcp_client.session import (  # noqa: F401  (re-exported for backward compatibility)
    _close_all_sessions,
    _evict_session,
    _get_or_create_session,
    _get_session_pool,
    _session_pool,
    _SessionPool,
    _sessions,
    _ssrf_safe_mcp_http_client,
)

# Public API surface for this package. Listing the supported symbols here makes
# the per-org MCP client discoverable and documents the stable facade. Note that
# `__all__` only governs `from mcp_client import *`; private helpers (prefixed
# with `_`, e.g. `_row_to_model`) remain importable by name for internal callers.
__all__ = [
    "OrgMcpServer",
    "init_mcp_client_db",
    "close_mcp_client_db",
    "create_org_mcp_server",
    "get_org_mcp_server",
    "list_org_mcp_servers",
    "update_org_mcp_server",
    "delete_org_mcp_server",
    "invalidate_mcp_cache",
    "discover_mcp_tools",
    "build_mcp_langchain_tools",
]
