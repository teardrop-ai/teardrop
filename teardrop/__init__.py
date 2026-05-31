# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Teardrop core application package.

Houses the FastAPI app bootstrap (``teardrop.app`` / ``teardrop.main``), HTTP
routers (``teardrop.routers``), and the auth, billing-adjacent, wallet, MCP
gateway, memory, usage, and config modules that make up the Teardrop service.

This package is intentionally a thin namespace: submodules are imported directly
(e.g. ``from teardrop.auth import require_auth``) rather than re-exported here,
which keeps import-time side effects and circular-import risk to a minimum.
"""
