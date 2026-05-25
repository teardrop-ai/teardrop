# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Compatibility entrypoint for the Teardrop FastAPI app.

The full application (routes, models, handlers) lives in teardrop.app.
This module preserves legacy imports and python -m teardrop.main behavior.
"""

from __future__ import annotations

import sys

from teardrop import app as _app_module

if __name__ != "__main__":
    # Preserve historical module semantics: teardrop.main behaves as a
    # compatibility alias of teardrop.app for imports and monkeypatching.
    sys.modules[__name__] = _app_module
else:
    from teardrop.config import get_settings

    settings = get_settings()

    import uvicorn

    uvicorn.run(
        "teardrop.app:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.app_log_level,
        reload=settings.app_env == "development",
    )
