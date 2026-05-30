# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Application metadata constants.

``APP_VERSION`` is the single source of truth for the FastAPI ``version`` and
for any router that previously read ``app.version`` directly (e.g. the system
discovery cards and the MCP JSON-RPC ``serverInfo`` block). Extracting it to a
dependency-free module lets routers reference the value without importing
``teardrop.app`` (which would create a circular import).
"""

from __future__ import annotations

APP_VERSION = "1.1.0"
