# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Initialize Neon database — create all application tables and verify connectivity.

Usage:
    python scripts/init_neon.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
from config import get_settings  # noqa: E402
from usage import close_usage_db, init_usage_db  # noqa: E402
from users import close_user_db, init_user_db  # noqa: E402


async def main() -> None:
    settings = get_settings()
    dsn = settings.pg_dsn
    if not dsn:
        print("ERROR: DATABASE_URL is not set in .env")
        sys.exit(1)

    print("Connecting to Postgres …")
    pool = await asyncpg.create_pool(dsn)

    # ── Application tables (asyncpg) ─────────────────────────────────────
    await init_user_db(pool)
    print("  ✓ orgs + users tables ready")

    await init_usage_db(pool)
    print("  ✓ usage_events table ready")

    # ── Checkpointer tables (psycopg3, via langgraph) ────────────────────
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()
    print("  ✓ checkpointer tables ready")

    # ── Verify ───────────────────────────────────────────────────────────
    tables = await pool.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    print(f"\nPublic tables ({len(tables)}):")
    for t in tables:
        print(f"  - {t['tablename']}")

    await close_usage_db()
    await close_user_db()
    await pool.close()
    print("\nDone — Neon database is ready.")


if __name__ == "__main__":
    asyncio.run(main())
