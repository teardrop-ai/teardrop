# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Lightweight SQL migration runner for Teardrop.

Applies numbered .sql files from migrations/versions/ in order.
Tracks applied migrations in a _migrations table.

Usage:
    python -m migrations.runner            # apply pending
    python -m migrations.runner --status   # show applied + pending
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_VERSIONS_DIR = Path(__file__).resolve().parent / "versions"

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS _migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _discover_migrations() -> list[Path]:
    """Return all .sql files in versions/, sorted by filename prefix."""
    files = sorted(_VERSIONS_DIR.glob("*.sql"), key=lambda p: p.name)
    return files


async def _get_applied(pool: asyncpg.Pool) -> set[str]:
    await pool.execute(_CREATE_TRACKING_TABLE)
    rows = await pool.fetch("SELECT version FROM _migrations ORDER BY version")
    return {r["version"] for r in rows}


async def apply_pending(pool: asyncpg.Pool) -> list[str]:
    """Apply all pending migrations. Returns the list of versions applied."""
    applied = await _get_applied(pool)
    all_files = _discover_migrations()
    newly_applied: list[str] = []

    for sql_file in all_files:
        version = sql_file.stem  # e.g. "001_baseline"
        if version in applied:
            logger.debug("Migration %s already applied — skipping", version)
            continue

        logger.info("Applying migration %s ...", version)
        sql = sql_file.read_text(encoding="utf-8")

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _migrations (version) VALUES ($1)", version
                )

        logger.info("Migration %s applied.", version)
        newly_applied.append(version)

    if not newly_applied:
        logger.debug("No pending migrations.")

    return newly_applied


async def get_status(pool: asyncpg.Pool) -> dict[str, list[str]]:
    """Return {'applied': [...], 'pending': [...]} for diagnostic use."""
    applied = await _get_applied(pool)
    all_versions = [f.stem for f in _discover_migrations()]
    pending = [v for v in all_versions if v not in applied]
    return {
        "applied": sorted(applied),
        "pending": pending,
    }


# ── CLI entry-point ───────────────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> None:
    from config import get_settings

    settings = get_settings()
    if not settings.pg_dsn:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    pool = await asyncpg.create_pool(settings.pg_dsn)
    try:
        if args.status:
            status = await get_status(pool)
            print("Applied migrations:")
            for v in status["applied"]:
                print(f"  ✓  {v}")
            print("Pending migrations:")
            for v in status["pending"]:
                print(f"  →  {v}")
            if not status["pending"]:
                print("  (none)")
        else:
            applied = await apply_pending(pool)
            if applied:
                print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
            else:
                print("No pending migrations.")
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Teardrop migration runner")
    parser.add_argument("--status", action="store_true", help="Show migration status without applying")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
