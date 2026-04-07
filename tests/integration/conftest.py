"""Integration test fixtures — spins up a Docker Postgres container.

All integration tests are skipped if Docker is not available or if
the SKIP_INTEGRATION_TESTS env var is set.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import asyncpg
import pytest


_TEST_DB_URL = os.getenv("DATABASE_URL", "")


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Skip when Docker is not available AND no TEST_DATABASE_URL is provided.
_can_run = bool(_TEST_DB_URL) or _docker_available()
if not _can_run or os.getenv("SKIP_INTEGRATION_TESTS"):
    pytest.skip(
        "No Postgres available for integration tests (set DATABASE_URL or run Docker).",
        allow_module_level=True,
    )


@pytest.fixture(scope="session")
def docker_postgres():
    """Provide a Postgres DSN for integration tests.

    - If DATABASE_URL is set (e.g. in CI with a service container), use it directly.
    - Otherwise, spin up a Docker container on port 5433.
    """
    if _TEST_DB_URL:
        # CI mode: Postgres is already running via GitHub Actions service container.
        yield _TEST_DB_URL
        return

    # Local mode: launch a throw-away container.
    container_name = f"teardrop-test-pg-{uuid.uuid4().hex[:8]}"
    dsn = "postgresql://postgres:testpass@localhost:5433/teardrop_test"

    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            "5433:5432",
            "-e",
            "POSTGRES_PASSWORD=testpass",
            "-e",
            "POSTGRES_DB=teardrop_test",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )

    # Wait for Postgres to be ready (up to 30 s).
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "pg_isready",
                    "-U",
                    "postgres",
                ],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.5)
    else:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        pytest.fail("Docker Postgres did not become ready in time.")

    yield dsn

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


@pytest.fixture
async def db_pool(docker_postgres: str):
    """Create an asyncpg pool, initialise all schemas, yield pool, truncate tables."""
    from users import init_user_db
    from wallets import init_wallets_db
    from usage import init_usage_db

    pool = await asyncpg.create_pool(docker_postgres, min_size=1, max_size=5)

    await init_user_db(pool)
    await init_wallets_db(pool)
    await init_usage_db(pool)

    yield pool

    # Truncate all tables so each test function gets a clean slate.
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE siwe_nonces, wallets, usage_events, users, orgs RESTART IDENTITY CASCADE"
        )

    await pool.close()
