# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""FastAPI router registration.

``register_routers(app)`` attaches every domain router to the application.
Routers are imported lazily inside the function to keep import ordering simple
and to avoid any possibility of a circular import with ``teardrop.app``.
"""

from __future__ import annotations

from fastapi import FastAPI


def register_routers(app: FastAPI) -> None:
    """Attach all domain routers to *app*. Order preserves OpenAPI grouping."""
    from teardrop.routers import admin, auth, billing, marketplace, system, wallets
    from teardrop.routers.org import a2a as org_a2a
    from teardrop.routers.org import llm_config as org_llm_config
    from teardrop.routers.org import mcp as org_mcp
    from teardrop.routers.org import memory as org_memory
    from teardrop.routers.org import tools as org_tools

    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(wallets.router)
    app.include_router(billing.router)
    app.include_router(org_tools.router)
    app.include_router(org_mcp.router)
    app.include_router(org_a2a.router)
    app.include_router(org_memory.router)
    app.include_router(org_llm_config.router)
    app.include_router(admin.router)
    app.include_router(marketplace.router)
