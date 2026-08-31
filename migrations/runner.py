# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
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
import re
import sys
from pathlib import Path

from shared.db_pool import PgConnection, PgPool, create_pool

logger = logging.getLogger(__name__)

_VERSIONS_DIR = Path(__file__).resolve().parent / "versions"
_ADVISORY_LOCK_KEY = 4_021_970
_VERSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

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


def _discover_squashes() -> dict[str, tuple[str, ...]]:
    """Return baseline stems mapped to the migration stems they replace."""
    squashes: dict[str, tuple[str, ...]] = {}
    owners: dict[str, str] = {}

    for manifest in sorted(_VERSIONS_DIR.glob("*.replaces"), key=lambda p: p.name):
        baseline = manifest.stem
        if not manifest.with_suffix(".sql").is_file():
            raise RuntimeError(f"Squash manifest {manifest.name} has no matching SQL baseline")

        replaced: list[str] = []
        for line_number, raw_line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
            version = raw_line.strip()
            if not version or version.startswith("#"):
                continue
            if not _VERSION_NAME_RE.fullmatch(version):
                raise RuntimeError(f"Invalid migration stem in {manifest.name}:{line_number}")
            if version == baseline:
                raise RuntimeError(f"Squash manifest {manifest.name} replaces itself")
            if version in replaced:
                raise RuntimeError(f"Duplicate migration {version} in {manifest.name}")
            previous_owner = owners.get(version)
            if previous_owner is not None:
                raise RuntimeError(f"Migration {version} is replaced by both {previous_owner} and {baseline}")
            owners[version] = baseline
            replaced.append(version)

        if not replaced:
            raise RuntimeError(f"Squash manifest {manifest.name} is empty")
        squashes[baseline] = tuple(replaced)

    return squashes


async def _get_applied(pool: PgPool | PgConnection) -> set[str]:
    await pool.execute(_CREATE_TRACKING_TABLE)
    rows = await pool.fetch("SELECT version FROM _migrations ORDER BY version")
    return {r["version"] for r in rows}


async def _record(conn: PgConnection, versions: list[str]) -> None:
    await conn.executemany(
        "INSERT INTO _migrations (version) VALUES ($1) ON CONFLICT (version) DO NOTHING",
        [(version,) for version in versions],
    )


async def _apply_pending(conn: PgConnection) -> list[str]:
    applied = await _get_applied(conn)
    all_files = _discover_migrations()
    squashes = _discover_squashes()
    superseded = {version for replaced in squashes.values() for version in replaced}
    newly_applied: list[str] = []

    for baseline, replaced in squashes.items():
        missing = [version for version in replaced if version not in applied]
        present = [version for version in replaced if version in applied]
        if baseline in applied:
            if missing:
                raise RuntimeError(f"Squash baseline {baseline} is applied with incomplete replacement history")
            continue
        if not present:
            continue
        if missing:
            raise RuntimeError(f"Partially applied migration history before {baseline}; missing replacement {missing[0]}")

        async with conn.transaction():
            await _record(conn, [baseline])
        applied.add(baseline)
        newly_applied.append(baseline)
        logger.info("Recorded squash baseline %s for existing schema.", baseline)

    for sql_file in all_files:
        version = sql_file.stem  # e.g. "001_baseline"
        if version in applied or version in superseded:
            logger.debug("Migration %s already applied — skipping", version)
            continue

        logger.info("Applying migration %s ...", version)
        sql = sql_file.read_text(encoding="utf-8")

        versions_to_record = [version, *squashes.get(version, ())]
        async with conn.transaction():
            await conn.execute(sql)
            await _record(conn, versions_to_record)

        logger.info("Migration %s applied.", version)
        applied.update(versions_to_record)
        newly_applied.append(version)

    if not newly_applied:
        logger.debug("No pending migrations.")

    return newly_applied


async def apply_pending(pool: PgPool) -> list[str]:
    """Apply all pending migrations while serializing database bootstraps."""
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_KEY)
        try:
            return await _apply_pending(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)


async def get_status(pool: PgPool) -> dict[str, list[str]]:
    """Return {'applied': [...], 'pending': [...]} for diagnostic use."""
    applied = await _get_applied(pool)
    squashes = _discover_squashes()
    superseded = {version for replaced in squashes.values() for version in replaced}
    all_versions = [f.stem for f in _discover_migrations() if f.stem not in superseded]
    pending = [v for v in all_versions if v not in applied]
    return {
        "applied": sorted(applied),
        "pending": pending,
    }


# ── CLI entry-point ───────────────────────────────────────────────────────────


async def _main(args: argparse.Namespace) -> None:
    from teardrop.config import get_settings

    settings = get_settings()
    if not settings.pg_dsn:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    pool = await create_pool(settings.pg_dsn)
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
