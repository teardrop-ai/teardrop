# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Tools package – versioned tool registry for the Teardrop agent."""

from __future__ import annotations

from tools.definitions import register_all
from tools.registry import ToolRegistry

# ─── Global registry singleton ────────────────────────────────────────────────

registry = ToolRegistry()
register_all(registry)


# ─── Backward-compatible API ──────────────────────────────────────────────────


def get_langchain_tools() -> list:
    """Return LangChain tools from the registry (backward-compatible)."""
    return registry.to_langchain_tools()


__all__ = ["registry", "get_langchain_tools"]
