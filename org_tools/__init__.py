# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-org custom webhook tool registry — CRUD, caching, encryption, SSRF validation,
circuit-breaker execution, marketplace publication, and LangChain wrapping.

Allows organisations to register webhook-backed tools that are injected into
the agent at run time alongside the global platform tool registry.

This package is split into focused submodules; this ``__init__`` is a stable
re-export facade so existing ``from org_tools import ...`` call sites keep working.

─── CRUD & lifecycle ─────────────────────────────────────────────────────────
:mod:`org_tools.crud` — create_org_tool, get_org_tool, list_org_tools,
update_org_tool, delete_org_tool.  Schema validation (JSON-Schema safe subset),
per-org quotas, unique-name enforcement, global tool name collision check,
marketplace publish_as_mcp flag.

─── Cache layer ──────────────────────────────────────────────────────────────
:mod:`org_tools.cache` — get_org_tools_cached (Redis TTLCache → in-process
fallback), invalidate_org_tools_cache after every mutation, list_marketplace_tools
(cached published tool list for agent injection).

─── Webhook execution runtime ────────────────────────────────────────────────
:mod:`org_tools.runtime` — _build_langchain_tool converts an OrgTool into a
LangChain StructuredTool.  SSRF validation enforced on every webhook call.
Auth header encryption/decryption (Fernet).  Circuit-breaker pattern via
tools.health.  50 KB response cap.  Immutable audit trail (_record_event).
_on_webhook_failure: Sentry capture + circuit-breaker + org deactivation.
build_org_langchain_tools assembles all active org tools, skips global name
collisions and non-GET webhook methods.

─── Foundation ───────────────────────────────────────────────────────────────
:mod:`org_tools.base` — OrgTool model, pool reference, constants, header
encryption, audit logging, row mapping.
"""

from __future__ import annotations

from org_tools.base import (  # noqa: F401  (re-exported for backward compatibility)
    _MAX_RESPONSE_BYTES,
    _ORG_TOOL_EVENT_INSERT_SQL,
    _POOL_SCOPE,
    _VALID_MARKETPLACE_CATEGORIES,
    OrgTool,
    _decrypt_header,
    _encrypt_header,
    _get_pool,
    _record_event,
    _row_to_org_tool,
    close_org_tools_db,
    init_org_tools_db,
)
from org_tools.cache import (  # noqa: F401  (re-exported for backward compatibility)
    _get_marketplace_lock,
    _get_org_tool_cache,
    _org_tool_caches,
    get_org_tools_cached,
    invalidate_marketplace_cache,
    invalidate_org_tools_cache,
    list_marketplace_tools,
)
from org_tools.crud import (  # noqa: F401  (re-exported for backward compatibility)
    create_org_tool,
    delete_org_tool,
    get_org_tool,
    list_org_tools,
    update_org_tool,
)
from org_tools.runtime import (  # noqa: F401  (re-exported for backward compatibility)
    _build_langchain_tool,
    _build_pydantic_model,
    _hash_webhook_host,
    _on_webhook_failure,
    build_org_langchain_tools,
    normalize_webhook_response,
    validate_safe_schema_subset,
)

# Public API surface for this package. Listing the supported symbols here makes
# the org-tool registry discoverable and documents the stable facade. Note that
# `__all__` only governs `from org_tools import *`; private helpers (prefixed
# with `_`) remain importable by name for internal callers.
__all__ = [
    "OrgTool",
    "init_org_tools_db",
    "close_org_tools_db",
    "create_org_tool",
    "get_org_tool",
    "list_org_tools",
    "update_org_tool",
    "delete_org_tool",
    "get_org_tools_cached",
    "invalidate_org_tools_cache",
    "list_marketplace_tools",
    "invalidate_marketplace_cache",
    "normalize_webhook_response",
    "validate_safe_schema_subset",
    "build_org_langchain_tools",
]
