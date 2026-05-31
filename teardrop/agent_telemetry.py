# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Lightweight RSS telemetry for agent-run memory-spike diagnosis.

Extracted from ``teardrop.routers.agent``. These helpers are diagnostic only:
they read ``/proc/self/status`` (Linux) or ``getrusage`` (fallback) and emit a
single structured log line per stage when
``settings.agent_memory_telemetry_enabled`` is set. They never affect request
handling.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from teardrop.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _process_rss_bytes() -> int | None:
    """Return current process RSS in bytes, or None when unavailable."""
    proc_status = Path("/proc/self/status")
    if proc_status.exists():
        try:
            for line in proc_status.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    # Format: VmRSS:\t  123456 kB
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
        except Exception:
            logger.debug("Failed reading /proc/self/status for RSS telemetry", exc_info=True)

    try:
        import resource  # noqa: PLC0415

        rss_raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if rss_raw <= 0:
            return None
        if sys.platform == "darwin":
            return rss_raw
        return rss_raw * 1024
    except Exception:
        return None


def _log_agent_memory(stage: str, *, run_id: str, elapsed_ms: int | None = None) -> None:
    """Emit lightweight RSS telemetry for memory-spike diagnosis."""
    if not settings.agent_memory_telemetry_enabled:
        return
    rss_bytes = _process_rss_bytes()
    if rss_bytes is None:
        return
    suffix = f" elapsed_ms={elapsed_ms}" if elapsed_ms is not None else ""
    logger.info(
        "agent_run memory stage=%s run_id=%s rss_mib=%.1f%s",
        stage,
        run_id,
        rss_bytes / (1024 * 1024),
        suffix,
    )
