# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Seed script — creates a default org and admin user for local development.

Usage:
    python scripts/seed_users.py

Prints the admin credentials to stdout. Re-running is safe — skips if the
default org already exists.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from config import get_settings  # noqa: E402
from users import close_user_db, create_org, create_user, init_user_db  # noqa: E402


async def main() -> None:
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.pg_dsn)
    await init_user_db(pool)

    # ── Create default org ────────────────────────────────────────────────
    try:
        org = await create_org("teardrop-default")
        print(f"Created org:  id={org.id}  name={org.name}")
    except asyncpg.UniqueViolationError:
        print("Default org already exists — skipping.")
        await close_user_db()
        await pool.close()
        return

    # ── Create admin user ─────────────────────────────────────────────────
    admin_secret = secrets.token_urlsafe(20)
    admin = await create_user(
        email="admin@teardrop.local",
        secret=admin_secret,
        org_id=org.id,
        role="admin",
    )
    print(f"Created admin: id={admin.id}  email={admin.email}  role={admin.role}")
    print(f"\n  *** Admin secret (save this): {admin_secret} ***\n")

    await close_user_db()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
