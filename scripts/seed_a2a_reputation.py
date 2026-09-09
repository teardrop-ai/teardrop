#!/usr/bin/env python3

# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Dry-run-first utility for building genuine public A2A reputation.

The default mode only validates the requested caller set and prints the
planned calls. ``--execute --confirm-live-calls`` performs real outbound
delegations through ``delegate_to_agent``. That path owns SSRF, allowlist,
budget, payment, settlement, refund, and immutable audit behavior; this
utility never inserts reputation or delegation rows itself.

Usage:
    python scripts/seed_a2a_reputation.py \
        --caller-org-id org-1 --caller-org-id org-2 --caller-org-id org-3 \
        --caller-org-id org-4 --caller-org-id org-5 \
        --target-url https://target.example.com \
        --task-description "Return a short capability check"

Add ``--execute --confirm-live-calls`` only after reviewing the dry-run plan.
Each caller organization must already have the target in its A2A allowlist and
must have enough configured billing capacity for the real delegation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from billing import close_billing, init_billing  # noqa: E402
from shared.db_pool import PgPool, create_pool  # noqa: E402
from teardrop.a2a_client import _canonicalize_agent_url, async_validate_url, check_delegation_allowed  # noqa: E402
from teardrop.config import get_settings  # noqa: E402
from tools.definitions.delegate_to_agent import delegate_to_agent  # noqa: E402

_MIN_CALLERS = 5
_TASK_TYPES = frozenset({"general", "research", "analysis", "data_retrieval", "coding", "transaction", "automation"})


def build_plan(
    caller_org_ids: Sequence[str],
    target_url: str,
    task_description: str,
    task_type: str = "general",
) -> dict[str, Any]:
    """Validate and return a side-effect-free execution plan."""
    callers = [str(org_id).strip() for org_id in caller_org_ids if str(org_id).strip()]
    if len(callers) < _MIN_CALLERS:
        raise ValueError(f"At least {_MIN_CALLERS} distinct caller organizations are required.")
    if len(set(callers)) != len(callers):
        raise ValueError("Caller organization IDs must be distinct.")

    try:
        normalized_target = _canonicalize_agent_url(target_url, require_https=True)
    except ValueError:
        raise ValueError("Target URL must be a valid HTTPS URL without credentials, query, or fragment.") from None

    description = str(task_description).strip()
    if not description or len(description) > 4096:
        raise ValueError("Task description must contain 1-4096 non-whitespace characters.")
    normalized_task_type = str(task_type).strip().lower()
    if normalized_task_type not in _TASK_TYPES:
        raise ValueError(f"Unsupported task type: {task_type}")

    return {
        "caller_org_ids": callers,
        "target_url": normalized_target,
        "task_description": description,
        "task_type": normalized_task_type,
    }


def plan_sha256(plan: dict[str, Any]) -> str:
    """Return a stable digest for the non-secret dry-run plan."""
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dry_run_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "call_count": len(plan["caller_org_ids"]),
        "caller_org_ids": plan["caller_org_ids"],
        "target_url": plan["target_url"],
        "task_type": plan["task_type"],
        "task_description": plan["task_description"],
        "plan_sha256": plan_sha256(plan),
        "next_step": "Review this plan, then rerun with --execute --confirm-live-calls.",
    }


async def _preflight(plan: dict[str, Any], pool: PgPool) -> None:
    settings = get_settings()
    if not settings.marketplace_enabled:
        raise RuntimeError("MARKETPLACE_ENABLED must be true for public A2A reputation to be visible.")
    if not settings.a2a_delegation_enabled:
        raise RuntimeError("A2A_DELEGATION_ENABLED must be true.")
    if not settings.a2a_delegation_billing_enabled:
        raise RuntimeError("A2A_DELEGATION_BILLING_ENABLED must be true so genuine events are audited.")

    ssrf_error = await async_validate_url(plan["target_url"])
    if ssrf_error:
        raise RuntimeError("Target URL failed SSRF validation.")

    target_row = await pool.fetchrow(
        "SELECT org_id FROM a2a_agent_registry WHERE rtrim(agent_url, '/') = $1",
        plan["target_url"],
    )
    if target_row is None:
        raise RuntimeError("Target URL is not registered in the public A2A agent directory.")
    target_org_id = str(target_row["org_id"])
    if target_org_id in plan["caller_org_ids"]:
        raise RuntimeError("The target organization cannot also be a caller organization.")

    for caller_org_id in plan["caller_org_ids"]:
        allowed, _ = await check_delegation_allowed(caller_org_id, plan["target_url"], pool)
        if not allowed:
            raise RuntimeError(f"Caller organization is not allowlisted for the target: {caller_org_id}")


async def _execute(plan: dict[str, Any], pg_dsn: str | None, principal_id: str) -> tuple[list[dict[str, Any]], bool]:
    settings = get_settings()
    pool = await create_pool(pg_dsn or settings.pg_dsn, min_size=1, max_size=2, name="a2a-reputation-seed")
    results: list[dict[str, Any]] = []
    execution_id = uuid.uuid4().hex
    try:
        await init_billing(pool)
        await _preflight(plan, pool)

        for index, caller_org_id in enumerate(plan["caller_org_ids"], start=1):
            run_id = f"a2a-reputation-seed-{execution_id}-{index}"
            config = {
                "configurable": {
                    "org_id": caller_org_id,
                    "db_pool": pool,
                    "run_id": run_id,
                    "principal_id": principal_id or "",
                }
            }
            try:
                result = await delegate_to_agent(
                    plan["target_url"],
                    plan["task_description"],
                    plan["task_type"],
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "caller_org_id": caller_org_id,
                        "status": "aborted",
                        "error": f"Unexpected delegation exception: {type(exc).__name__}",
                    }
                )
                break

            results.append(
                {
                    "caller_org_id": caller_org_id,
                    "agent_name": result.get("agent_name", ""),
                    "status": result.get("status", "failed"),
                    "cost_usdc": int(result.get("cost_usdc", 0) or 0),
                    "error": result.get("error"),
                }
            )
    finally:
        try:
            await close_billing()
        finally:
            await pool.close()

    succeeded = bool(results) and len(results) == len(plan["caller_org_ids"]) and all(
        result["status"] == "completed" for result in results
    )
    return results, succeeded


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--caller-org-id",
        action="append",
        required=True,
        help="Caller organization ID; repeat at least five times with distinct IDs.",
    )
    parser.add_argument("--target-url", required=True, help="Registered HTTPS A2A base URL to call.")
    parser.add_argument("--task-description", required=True, help="Bounded task sent through delegate_to_agent.")
    parser.add_argument("--task-type", choices=sorted(_TASK_TYPES), default="general")
    parser.add_argument("--principal-id", default="", help="Optional caller principal used by existing spend limits.")
    parser.add_argument("--pg-dsn", default=None, help="Optional Postgres DSN; otherwise use the configured DATABASE_URL.")
    parser.add_argument("--execute", action="store_true", help="Perform real outbound delegations.")
    parser.add_argument(
        "--confirm-live-calls",
        action="store_true",
        help="Required with --execute to acknowledge real paid/network calls.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the dry-run or execution result as JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.execute and not args.confirm_live_calls:
        parser.error("--execute requires --confirm-live-calls")

    try:
        plan = build_plan(args.caller_org_id, args.target_url, args.task_description, args.task_type)
    except ValueError as exc:
        parser.error(str(exc))

    if not args.execute:
        payload = _dry_run_payload(plan)
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else json.dumps(payload, indent=2))
        return 0

    try:
        results, succeeded = asyncio.run(_execute(plan, args.pg_dsn, args.principal_id))
    except Exception as exc:  # noqa: BLE001
        print(
            f"Execution aborted ({type(exc).__name__}); no synthetic reputation rows were written.",
            file=sys.stderr,
        )
        return 1

    payload = {
        "mode": "execute",
        "plan_sha256": plan_sha256(plan),
        "results": results,
        "succeeded": succeeded,
    }
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else json.dumps(payload, indent=2))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
