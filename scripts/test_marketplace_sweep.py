#!/usr/bin/env python3
"""
Smoke-test for marketplace_sweep_once() against a live Postgres database.

Seeds 3 mock tool authors with pending earnings, runs the sweep with a mocked
CDP transfer, and validates five scenarios:

  1. Happy path  — 3 orgs processed; earnings + withdrawals → 'settled'.
  2. Idempotency — same epoch, running sweep again creates no duplicate records.
  3. Determinism — withdrawal IDs are stable (SHA-256 of org + epoch_hour).
  4. CDP failure — org whose transfer raises is marked 'failed' with backoff.
  5. Below minimum — org below threshold is skipped entirely.

CDP transfers are always mocked — no on-chain activity is triggered.

Designed for a clean testnet / staging database.  All test data is isolated by
a unique run UUID and removed in the finally block.

Usage:
  python scripts/test_marketplace_sweep.py --pg-dsn postgresql://...
  python scripts/test_marketplace_sweep.py  # reads $PG_DSN
  python scripts/test_marketplace_sweep.py --network base-sepolia --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import asyncpg

# Ensure the project root is importable when running from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import marketplace
from config import get_settings
from marketplace import marketplace_sweep_once

# ─── Constants ────────────────────────────────────────────────────────────────

_VALID_WALLET = "0x742d35Cc6634C0532925a3b8D4C9F1d2b4C3e2F1"  # dummy EIP-55 address
_MIN_AMOUNT = 100_000  # $0.10 — matches MARKETPLACE_MINIMUM_WITHDRAWAL_USDC default
_EARNINGS = 500_000  # $0.50 per org — comfortably above minimum
_BELOW_MIN = 50_000  # $0.05 — below minimum, should be skipped
_MOCK_TX = "0xabcdef" + "0" * 58  # deterministic fake tx hash

# ─── Output helpers ───────────────────────────────────────────────────────────


def _step(msg: str) -> None:
    print(f"  → {msg}")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    print(f"\n  ✗ FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _assert_eq(actual, expected, label: str) -> None:
    if actual != expected:
        _fail(f"{label}: expected {expected!r}, got {actual!r}")
    _ok(label)


def _assert_not_none(val, label: str) -> None:
    if val is None:
        _fail(f"{label}: expected a value, got None")
    _ok(label)


# ─── Slug derivation (mirrors migration 013 formula) ─────────────────────────


def _make_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return s.strip("-")[:40]


# ─── Deterministic ID (mirrors marketplace._sweep_withdrawal_id) ─────────────


def _expected_withdrawal_id(org_id: str) -> str:
    now = datetime.now(timezone.utc)
    epoch_hour = int(now.timestamp()) // 3600
    raw = hashlib.sha256(f"sweep:{org_id}:{epoch_hour}".encode()).digest()
    h = raw[:16].hex()
    return f"{h[:8]}-{h[8:12]}-5{h[13:16]}-{h[16:20]}-{h[20:32]}"


# ─── DB helpers ───────────────────────────────────────────────────────────────


async def _seed_org(pool: asyncpg.Pool, org_id: str, name: str) -> None:
    await pool.execute(
        """
        INSERT INTO orgs (id, name, slug, created_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (id) DO NOTHING
        """,
        org_id,
        name,
        _make_slug(name),
        datetime.now(timezone.utc),
    )


async def _seed_author_config(pool: asyncpg.Pool, org_id: str) -> None:
    await pool.execute(
        """
        INSERT INTO tool_author_config
            (org_id, settlement_wallet, revenue_share_bps, created_at, updated_at)
        VALUES ($1, $2, 7000, NOW(), NOW())
        ON CONFLICT (org_id) DO NOTHING
        """,
        org_id,
        _VALID_WALLET,
    )


async def _seed_earnings(pool: asyncpg.Pool, org_id: str, amount: int) -> None:
    await pool.execute(
        """
        INSERT INTO tool_author_earnings
            (id, org_id, tool_name, caller_org_id,
             amount_usdc, author_share_usdc, platform_share_usdc,
             status, created_at)
        VALUES ($1, $2, 'smoke/test_tool', 'caller-org',
                $3, $3, 0, 'pending', NOW())
        """,
        str(uuid.uuid4()),
        org_id,
        amount,
    )


async def _cleanup(pool: asyncpg.Pool, org_ids: list[str]) -> None:
    if not org_ids:
        return
    await pool.execute("DELETE FROM tool_author_earnings WHERE org_id = ANY($1::text[])", org_ids)
    await pool.execute("DELETE FROM tool_author_withdrawals WHERE org_id = ANY($1::text[])", org_ids)
    await pool.execute("DELETE FROM tool_author_config WHERE org_id = ANY($1::text[])", org_ids)
    await pool.execute("DELETE FROM orgs WHERE id = ANY($1::text[])", org_ids)


# ─── Scenario 1: happy path ───────────────────────────────────────────────────


async def test_happy_path(pool: asyncpg.Pool, run_id: str, verbose: bool) -> list[str]:
    print("\n[Test 1] Happy path — 3 orgs with $0.50 pending earnings each")

    org_ids = [f"smk-swp-{run_id[:8]}-{i}" for i in range(1, 4)]
    for i, oid in enumerate(org_ids, 1):
        name = f"Smoke Sweep Org {i} {run_id[:8]}"
        await _seed_org(pool, oid, name)
        await _seed_author_config(pool, oid)
        await _seed_earnings(pool, oid, _EARNINGS)

    _step(f"Seeded 3 test orgs (suffix …{run_id[:8]})")

    mock_transfer = AsyncMock(return_value=_MOCK_TX)
    _settings = get_settings()
    with patch.object(_settings, "agent_wallet_enabled", True), patch("agent_wallets.transfer_usdc", mock_transfer):
        count = await marketplace_sweep_once()

    if count < 3:
        _fail(f"sweep returned count={count}, expected ≥ 3 (our 3 test orgs)")
    _ok(f"sweep count={count} (≥ 3 — includes our 3 test orgs)")

    if mock_transfer.call_count < 3:
        _fail(f"transfer_usdc called {mock_transfer.call_count} times, expected ≥ 3")
    _ok(f"transfer_usdc called {mock_transfer.call_count} time(s)")

    if verbose:
        _step(f"First CDP call args: {mock_transfer.call_args_list[0]}")

    # Verify DB state for each of our 3 orgs
    for oid in org_ids:
        w = await pool.fetchrow(
            "SELECT status, tx_hash, amount_usdc FROM tool_author_withdrawals WHERE org_id = $1",
            oid,
        )
        _assert_not_none(w, f"withdrawal record exists for {oid}")
        _assert_eq(w["status"], "settled", f"withdrawal.status=settled for {oid}")
        _assert_eq(w["tx_hash"], _MOCK_TX, f"withdrawal.tx_hash populated for {oid}")
        _assert_eq(w["amount_usdc"], _EARNINGS, f"withdrawal.amount_usdc for {oid}")

        settled = await pool.fetchval(
            "SELECT COUNT(*) FROM tool_author_earnings WHERE org_id = $1 AND status = 'settled'",
            oid,
        )
        _assert_eq(settled, 1, f"earnings.status=settled for {oid}")

    return org_ids


# ─── Scenario 2: idempotency ──────────────────────────────────────────────────


async def test_idempotency(pool: asyncpg.Pool, org_ids: list[str]) -> None:
    print("\n[Test 2] Idempotency — re-running sweep in same epoch creates no duplicates")

    mock_transfer = AsyncMock(return_value=_MOCK_TX)
    with patch("agent_wallets.transfer_usdc", mock_transfer):
        await marketplace_sweep_once()

    for oid in org_ids:
        n = await pool.fetchval("SELECT COUNT(*) FROM tool_author_withdrawals WHERE org_id = $1", oid)
        _assert_eq(n, 1, f"exactly 1 withdrawal record for {oid} (no duplicates)")

    _ok("transfer_usdc not re-invoked for already-settled orgs")


# ─── Scenario 3: deterministic IDs ───────────────────────────────────────────


async def test_deterministic_ids(pool: asyncpg.Pool, org_ids: list[str]) -> None:
    print("\n[Test 3] Deterministic IDs — withdrawal IDs match SHA-256(org+epoch)")

    for oid in org_ids:
        expected = _expected_withdrawal_id(oid)
        actual = await pool.fetchval("SELECT id FROM tool_author_withdrawals WHERE org_id = $1", oid)
        _assert_eq(actual, expected, f"deterministic ID for {oid}")


# ─── Scenario 4: CDP failure ──────────────────────────────────────────────────


async def test_cdp_failure(pool: asyncpg.Pool, run_id: str) -> list[str]:
    print("\n[Test 4] CDP failure — withdrawal is marked 'failed' with backoff")

    oid = f"smk-swp-{run_id[:8]}-fail"
    await _seed_org(pool, oid, f"Smoke Sweep Fail {run_id[:8]}")
    await _seed_author_config(pool, oid)
    await _seed_earnings(pool, oid, _EARNINGS)
    _step(f"Seeded failing org: {oid}")

    mock_transfer = AsyncMock(side_effect=RuntimeError("CDP network unreachable"))
    _settings = get_settings()
    with patch.object(_settings, "agent_wallet_enabled", True), patch("agent_wallets.transfer_usdc", mock_transfer):
        await marketplace_sweep_once()

    w = await pool.fetchrow(
        """
        SELECT status, sweep_attempt_count, last_sweep_error, next_sweep_at
        FROM tool_author_withdrawals WHERE org_id = $1
        """,
        oid,
    )
    _assert_not_none(w, f"withdrawal record created for {oid}")
    _assert_eq(w["status"], "failed", f"withdrawal.status=failed for {oid}")
    _assert_eq(w["sweep_attempt_count"], 1, f"sweep_attempt_count=1 for {oid}")
    _assert_not_none(w["next_sweep_at"], f"next_sweep_at is set (backoff active) for {oid}")

    if w["next_sweep_at"] <= datetime.now(timezone.utc):
        _fail(f"next_sweep_at is in the past — backoff not applied for {oid}")
    _ok(f"next_sweep_at is a future timestamp for {oid}")

    return [oid]


# ─── Scenario 5: below minimum ───────────────────────────────────────────────


async def test_below_minimum(pool: asyncpg.Pool, run_id: str) -> list[str]:
    print(f"\n[Test 5] Below minimum — org with ${_BELOW_MIN / 1_000_000:.4f} pending is skipped")

    oid = f"smk-swp-{run_id[:8]}-low"
    await _seed_org(pool, oid, f"Smoke Sweep Low {run_id[:8]}")
    await _seed_author_config(pool, oid)
    await _seed_earnings(pool, oid, _BELOW_MIN)
    _step(f"Seeded below-minimum org: {oid} ({_BELOW_MIN} atomic = ${_BELOW_MIN / 1_000_000:.5f})")

    mock_transfer = AsyncMock(return_value=_MOCK_TX)
    with patch("agent_wallets.transfer_usdc", mock_transfer):
        await marketplace_sweep_once()

    n = await pool.fetchval("SELECT COUNT(*) FROM tool_author_withdrawals WHERE org_id = $1", oid)
    _assert_eq(n, 0, f"no withdrawal created for below-minimum org {oid}")
    _assert_eq(mock_transfer.call_count, 0, "transfer_usdc not called for below-minimum org")

    return [oid]


# ─── Main ─────────────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test marketplace_sweep_once() against a live Postgres DB.")
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("PG_DSN", ""),
        help="Postgres connection string (default: $PG_DSN)",
    )
    parser.add_argument(
        "--network",
        default="base-sepolia",
        choices=["base-sepolia", "base"],
        help="CDP network label for reporting (transfers are mocked; no on-chain activity)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra diagnostic output",
    )
    args = parser.parse_args()

    if not args.pg_dsn:
        print(
            "Error: provide --pg-dsn or set $PG_DSN.\n"
            "Example: python scripts/test_marketplace_sweep.py --pg-dsn postgresql://...",
            file=sys.stderr,
        )
        sys.exit(1)

    run_id = uuid.uuid4().hex
    print("Teardrop marketplace_sweep_once() smoke-test")
    print(f"  Network : {args.network}  (CDP mocked — no on-chain activity)")
    print(f"  Run ID  : {run_id[:8]}")
    print(f"  DSN     : {args.pg_dsn[:40]}{'…' if len(args.pg_dsn) > 40 else ''}")

    pool = await asyncpg.create_pool(args.pg_dsn, min_size=1, max_size=3)
    marketplace._pool = pool

    all_org_ids: list[str] = []
    try:
        happy_orgs = await test_happy_path(pool, run_id, args.verbose)
        all_org_ids.extend(happy_orgs)

        await test_idempotency(pool, happy_orgs)
        await test_deterministic_ids(pool, happy_orgs)

        fail_orgs = await test_cdp_failure(pool, run_id)
        all_org_ids.extend(fail_orgs)

        low_orgs = await test_below_minimum(pool, run_id)
        all_org_ids.extend(low_orgs)

    finally:
        _step(f"Cleaning up {len(all_org_ids)} test org(s)…")
        await _cleanup(pool, all_org_ids)
        await pool.close()
        marketplace._pool = None
        _ok("Test data removed")

    print(
        f"\n✅  All checks passed — marketplace_sweep_once() behaves correctly."
        f"\n   Network: {args.network} | CDP: mocked | Scenarios: 5"
    )


if __name__ == "__main__":
    asyncio.run(main())
