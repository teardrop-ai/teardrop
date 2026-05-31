# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin-only routes package.

All routes require the ``require_admin`` dependency. The single legacy
``admin.py`` module was split into domain sub-modules for ownership clarity;
URL paths are unchanged. Sub-modules:

  - identity:    org/user/client-credential provisioning
  - usage:       per-user and per-org usage reporting
  - billing:     tool pricing, revenue, credit top-ups, settlement, spending
  - tools:       org tool & MCP server inspection
  - memory:      org memory inspection & purge
  - a2a:         A2A delegation allowlist management
  - marketplace: withdrawal & settlement operations

The master ``router`` mounts each sub-module's router so registration in
``teardrop.routers.__init__`` (``admin.router``) keeps working unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from teardrop.routers.admin import (
    a2a,
    billing,
    identity,
    marketplace,
    memory,
    tools,
    usage,
)

router = APIRouter()
router.include_router(identity.router)
router.include_router(usage.router)
router.include_router(billing.router)
router.include_router(tools.router)
router.include_router(memory.router)
router.include_router(a2a.router)
router.include_router(marketplace.router)

__all__ = ["router"]
