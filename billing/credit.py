# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Credit subsystem for billing (off-chain prepaid balances)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

import sentry_sdk

from shared.db_pool import PgConnection, PgPool, UniqueViolation
from teardrop.cache import TTLCache
from teardrop.config import get_settings
from teardrop.users.credentials import create_client_credential_in_transaction
from teardrop.users.models import OrgClientCredential

logger = logging.getLogger(__name__)

_ONBOARDING_GRANT_REASON = "onboarding_grant"


class BillingCreditService:
    """Encapsulates org credit balance verification and mutations."""

    def __init__(
        self,
        *,
        get_pool: Callable[[], PgPool],
        get_daily_spend_cache: Callable[[str], TTLCache[int]],
        get_daily_debit_spend_fn: Callable[[PgConnection | PgPool, str], Awaitable[int]],
        billing_result_factory: Callable[..., Any],
    ):
        self._get_pool = get_pool
        self._get_daily_spend_cache = get_daily_spend_cache
        self._get_daily_debit_spend_fn = get_daily_debit_spend_fn
        self._billing_result_factory = billing_result_factory

    async def get_credit_balance(self, org_id: str) -> int:
        """Return org's current credit balance in atomic USDC (0 if no row yet)."""
        pool = self._get_pool()
        row = await pool.fetchrow(
            "SELECT balance_usdc FROM org_credits WHERE org_id = $1",
            org_id,
        )
        return int(row["balance_usdc"]) if row is not None else 0

    async def get_daily_debit_spend(self, executor: PgConnection | PgPool, org_id: str) -> int:
        """Return 24h rolling debit spend in atomic USDC for an org."""
        daily_row = await executor.fetchrow(
            """
            SELECT COALESCE(SUM(amount_usdc), 0) AS daily_spend
            FROM org_credit_ledger
            WHERE org_id = $1
              AND operation = 'debit'
              AND created_at >= NOW() - INTERVAL '24 hours'
            """,
            org_id,
        )
        return int(daily_row["daily_spend"]) if daily_row else 0

    async def verify_credit(self, org_id: str, min_balance_usdc: int) -> Any:
        """Check that org has sufficient credit and is within spending limits."""
        pool = self._get_pool()

        row = await pool.fetchrow(
            """
            SELECT c.balance_usdc, c.spending_limit_usdc, c.is_paused,
                   o.acquisition_source
            FROM org_credits AS c
            JOIN orgs AS o ON o.id = c.org_id
            WHERE c.org_id = $1
            """,
            org_id,
        )
        balance = int(row["balance_usdc"]) if row else 0
        spending_limit = int(row["spending_limit_usdc"]) if row else 0
        is_paused = bool(row["is_paused"]) if row else False
        machine_org = bool(row and row.get("acquisition_source") in {"siwe", "x402"})
        if machine_org and spending_limit <= 0:
            machine_cap = get_settings().machine_org_daily_spend_limit_usdc
            spending_limit = machine_cap

        if is_paused:
            return self._billing_result_factory(error="Org billing is paused by admin. Contact your administrator.")

        if balance < min_balance_usdc:
            if machine_org:
                return self._billing_result_factory(
                    error=(
                        f"Insufficient credit: balance {balance} atomic USDC, required {min_balance_usdc}. "
                        "Fund via GET /billing/topup/usdc/requirements then POST /billing/topup/usdc, "
                        "or POST /token with grant_type=x402."
                    )
                )
            return self._billing_result_factory(
                error=(
                    f"Insufficient credit: balance {balance} atomic USDC, "
                    f"required {min_balance_usdc}. Top up via POST /admin/credits/topup."
                )
            )

        if spending_limit > 0:
            daily_spend = await self._get_daily_debit_spend_fn(pool, org_id)
            if daily_spend + min_balance_usdc > spending_limit:
                return self._billing_result_factory(
                    error=(
                        f"Daily spending limit reached: {daily_spend} of {spending_limit} atomic USDC used in the last 24 hours."
                    )
                )

        return self._billing_result_factory(verified=True, billing_method="credit")

    async def _debit_credit_locked(
        self,
        conn: PgConnection,
        org_id: str,
        amount_usdc: int,
        reason: str,
    ) -> tuple[bool, int]:
        """Debit one org while the caller owns the surrounding transaction."""
        row = await conn.fetchrow(
            """
            SELECT c.balance_usdc, c.spending_limit_usdc, c.is_paused,
                   o.acquisition_source
            FROM org_credits AS c
            JOIN orgs AS o ON o.id = c.org_id
            WHERE c.org_id = $1
            FOR UPDATE OF c
            """,
            org_id,
        )
        if row is None:
            return False, 0

        original_balance = int(row["balance_usdc"])
        spending_limit = int(row["spending_limit_usdc"])
        is_paused = bool(row["is_paused"])
        if row.get("acquisition_source") in {"siwe", "x402"} and spending_limit <= 0:
            machine_cap = get_settings().machine_org_daily_spend_limit_usdc
            spending_limit = machine_cap

        if is_paused:
            return False, 0

        if spending_limit > 0:
            daily_spend = await self._get_daily_debit_spend_fn(conn, org_id)
            if daily_spend + amount_usdc > spending_limit:
                return False, 0

        # Never partially settle. The row lock closes the concurrent-debit race
        # between the non-locking preflight and this authoritative mutation.
        if original_balance < amount_usdc:
            return False, 0

        new_balance = original_balance - amount_usdc
        await conn.execute(
            """
            UPDATE org_credits
            SET balance_usdc = $2, updated_at = NOW()
            WHERE org_id = $1
            """,
            org_id,
            new_balance,
        )
        await conn.execute(
            """
            INSERT INTO org_credit_ledger
                (id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at)
            VALUES ($1, $2, 'debit', $3, $4, $5, NOW())
            """,
            str(uuid.uuid4()),
            org_id,
            amount_usdc,
            new_balance,
            reason,
        )
        return True, amount_usdc

    async def debit_credit(self, org_id: str, amount_usdc: int, reason: str = "") -> tuple[bool, int]:
        """Debit amount_usdc from org's credit balance atomically."""
        if amount_usdc <= 0:
            logger.debug("debit_credit: skipping non-positive amount org_id=%s amount=%s", org_id, amount_usdc)
            return True, 0

        pool = self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    success, actual_deducted = await self._debit_credit_locked(conn, org_id, amount_usdc, reason)
                if success:
                    try:
                        await self._get_daily_spend_cache(org_id).invalidate()
                    except Exception:
                        logger.warning("debit_credit: cache invalidation failed org_id=%s", org_id, exc_info=True)
            return success, actual_deducted
        except Exception as exc:
            logger.exception("debit_credit failed org_id=%s amount=%s", org_id, amount_usdc)
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("org_id", str(org_id))
                scope.set_tag("amount_usdc_atomic", str(amount_usdc))
                scope.set_tag("rail", "credit")
                sentry_sdk.capture_exception(exc)
            return False, 0

    async def debit_credit_with_delegation_refund(
        self,
        org_id: str,
        amount_usdc: int,
        reason: str,
        delegation_id: str,
        run_id: str,
    ) -> tuple[bool, int]:
        """Debit credit and create the delegation refund record in one transaction."""
        if amount_usdc <= 0:
            return True, 0

        pool = self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    existing = await conn.fetchrow(
                        """
                        SELECT status, amount_usdc
                        FROM a2a_delegation_refund_outbox
                        WHERE id = $1 AND org_id = $2
                        FOR UPDATE
                        """,
                        delegation_id,
                        org_id,
                    )
                    if existing is not None:
                        if int(existing["amount_usdc"]) != amount_usdc:
                            raise ValueError("Delegation funding amount changed for an existing id")
                        return existing["status"] == "pending", int(existing["amount_usdc"])

                    success, actual_deducted = await self._debit_credit_locked(conn, org_id, amount_usdc, reason)
                    if not success:
                        return False, 0

                    await conn.execute(
                        """
                        INSERT INTO a2a_delegation_refund_outbox
                            (id, org_id, run_id, amount_usdc, status, created_at)
                        VALUES ($1, $2, $3, $4, 'pending', NOW())
                        """,
                        delegation_id,
                        org_id,
                        run_id,
                        actual_deducted,
                    )
                try:
                    await self._get_daily_spend_cache(org_id).invalidate()
                except Exception:
                    logger.warning(
                        "delegation funding: cache invalidation failed org_id=%s delegation=%s",
                        org_id,
                        delegation_id,
                        exc_info=True,
                    )
            return True, actual_deducted
        except Exception as exc:
            logger.exception("delegation credit funding failed org_id=%s amount=%s", org_id, amount_usdc)
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("org_id", str(org_id))
                scope.set_tag("amount_usdc_atomic", str(amount_usdc))
                scope.set_tag("rail", "credit")
                sentry_sdk.capture_exception(exc)
            return False, 0

    async def admin_topup_credit(
        self,
        org_id: str,
        amount_usdc: int,
        reason: str = "",
        external_ref: str | None = None,
    ) -> int:
        """Add amount_usdc to org's credit balance (upsert).

        When ``external_ref`` is set, the topup is idempotent: a duplicate ref
        (e.g. a replayed x402 onboarding payment) is a no-op returning the
        current balance, enforced by the partial unique index from migration 097.
        """
        pool = self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        """
                        INSERT INTO org_credits (org_id, balance_usdc, updated_at)
                        VALUES ($1, $2, NOW())
                        ON CONFLICT (org_id) DO UPDATE
                            SET balance_usdc = org_credits.balance_usdc + EXCLUDED.balance_usdc,
                                updated_at = NOW()
                        RETURNING balance_usdc
                        """,
                        org_id,
                        amount_usdc,
                    )
                    new_balance = int(row["balance_usdc"])
                    await conn.execute(
                        """
                        INSERT INTO org_credit_ledger
                            (id, org_id, operation, amount_usdc, balance_usdc_after, reason, external_ref, created_at)
                        VALUES ($1, $2, 'topup', $3, $4, $5, $6, NOW())
                        """,
                        str(uuid.uuid4()),
                        org_id,
                        amount_usdc,
                        new_balance,
                        reason,
                        external_ref,
                    )
            return new_balance
        except UniqueViolation:
            if external_ref is None:
                raise
            existing = await pool.fetchrow(
                "SELECT org_id, amount_usdc FROM org_credit_ledger WHERE external_ref = $1",
                external_ref,
            )
            if existing is None or existing["org_id"] != org_id or int(existing["amount_usdc"]) != amount_usdc:
                raise
            current = await pool.fetchrow(
                "SELECT balance_usdc FROM org_credits WHERE org_id = $1",
                org_id,
            )
            return int(current["balance_usdc"]) if current is not None else 0

    async def record_onboarding_settlement(
        self,
        org_id: str,
        amount_usdc: int,
        external_ref: str,
        payer_address: str,
        chain_id: int,
        settlement_tx: str,
    ) -> tuple[OrgClientCredential, str | None]:
        """Credit a settled bootstrap payment, credential, and audit atomically."""
        if amount_usdc <= 0:
            raise ValueError("onboarding settlement amount must be positive")
        if not external_ref or not settlement_tx:
            raise ValueError("onboarding settlement requires payment and transaction references")

        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchrow("SELECT id FROM orgs WHERE id = $1 FOR UPDATE", org_id)
                existing = await conn.fetchrow(
                    "SELECT org_id, amount_usdc FROM org_credit_ledger WHERE external_ref = $1 FOR UPDATE",
                    external_ref,
                )
                if existing is not None:
                    if existing["org_id"] != org_id or int(existing["amount_usdc"]) != amount_usdc:
                        raise ValueError("Onboarding payment reference is bound to a different settlement")
                    credential = await conn.fetchrow(
                        "SELECT client_id, org_id, hashed_secret, salt, created_at "
                        "FROM org_client_credentials WHERE org_id = $1 ORDER BY created_at ASC LIMIT 1",
                        org_id,
                    )
                    if credential is not None:
                        return (
                            OrgClientCredential(
                                client_id=credential["client_id"],
                                org_id=credential["org_id"],
                                hashed_secret=credential["hashed_secret"],
                                salt=credential["salt"],
                                created_at=credential["created_at"],
                            ),
                            None,
                        )
                    return await create_client_credential_in_transaction(conn, org_id)

                existing_credential = await conn.fetchrow(
                    "SELECT client_id, org_id, hashed_secret, salt, created_at "
                    "FROM org_client_credentials WHERE org_id = $1 ORDER BY created_at ASC LIMIT 1",
                    org_id,
                )
                if existing_credential is not None:
                    credential = OrgClientCredential(
                        client_id=existing_credential["client_id"],
                        org_id=existing_credential["org_id"],
                        hashed_secret=existing_credential["hashed_secret"],
                        salt=existing_credential["salt"],
                        created_at=existing_credential["created_at"],
                    )
                    client_secret = None
                else:
                    credential, client_secret = await create_client_credential_in_transaction(conn, org_id)
                row = await conn.fetchrow(
                    """
                    INSERT INTO org_credits (org_id, balance_usdc, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (org_id) DO UPDATE
                        SET balance_usdc = org_credits.balance_usdc + EXCLUDED.balance_usdc,
                            updated_at = NOW()
                    RETURNING balance_usdc
                    """,
                    org_id,
                    amount_usdc,
                )
                new_balance = int(row["balance_usdc"])
                await conn.execute(
                    """
                    INSERT INTO org_credit_ledger
                        (id, org_id, operation, amount_usdc, balance_usdc_after, reason, external_ref, created_at)
                    VALUES ($1, $2, 'topup', $3, $4, 'x402_onboarding', $5, NOW())
                    """,
                    str(uuid.uuid4()),
                    org_id,
                    amount_usdc,
                    new_balance,
                    external_ref,
                )
                await conn.execute(
                    """
                    INSERT INTO org_provisioning_events
                        (id, org_id, method, payer_address, chain_id, settlement_tx,
                         payment_ref, amount_usdc, event_type, settlement_status)
                    VALUES ($1, $2, 'x402', $3, $4, $5, $6, $7, 'settlement', 'settled')
                    """,
                    str(uuid.uuid4()),
                    org_id,
                    payer_address,
                    chain_id,
                    settlement_tx,
                    external_ref,
                    amount_usdc,
                )
            return credential, client_secret

    async def grant_onboarding_credit(self, org_id: str, amount_usdc: int) -> int:
        """Grant one idempotent verified-email credit balance in one transaction.

        The grant marker, balance upsert, and immutable ledger row intentionally
        share a transaction. A duplicate org marker is a no-op, while any
        failure rolls back the marker so a later retry can safely recover.
        Returns the amount granted, or ``0`` when the grant already exists.
        """
        if amount_usdc <= 0:
            raise ValueError("onboarding credit amount must be positive")

        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                ledger_entry_id = str(uuid.uuid4())
                grant_row = await conn.fetchrow(
                    """
                    INSERT INTO org_onboarding_credit_grants (org_id, amount_usdc, ledger_entry_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (org_id) DO NOTHING
                    RETURNING amount_usdc
                    """,
                    org_id,
                    amount_usdc,
                    ledger_entry_id,
                )
                if grant_row is None:
                    return 0

                granted_amount = int(grant_row["amount_usdc"])
                credit_row = await conn.fetchrow(
                    """
                    INSERT INTO org_credits (org_id, balance_usdc, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (org_id) DO UPDATE
                        SET balance_usdc = org_credits.balance_usdc + EXCLUDED.balance_usdc,
                            updated_at = NOW()
                    RETURNING balance_usdc
                    """,
                    org_id,
                    granted_amount,
                )
                new_balance = int(credit_row["balance_usdc"])
                await conn.execute(
                    """
                    INSERT INTO org_credit_ledger
                        (id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at)
                    VALUES ($1, $2, 'topup', $3, $4, $5, NOW())
                    """,
                    ledger_entry_id,
                    org_id,
                    granted_amount,
                    new_balance,
                    _ONBOARDING_GRANT_REASON,
                )
        return granted_amount

    async def clear_onboarding_credit_outbox(self, org_id: str) -> None:
        """Remove a completed/no-longer-needed onboarding-credit outbox entry."""
        pool = self._get_pool()
        await pool.execute(
            "DELETE FROM org_onboarding_credit_outbox WHERE org_id = $1",
            org_id,
        )

    async def process_onboarding_credit_outbox(self, limit: int = 50) -> int:
        """Retry queued onboarding-credit grants; returns the count successfully granted.

        Each pending outbox row is retried via the idempotent
        ``grant_onboarding_credit``, whose unique ``org_id`` marker makes
        concurrent/duplicate processing safe (a second attempt is simply a
        no-op). On success the outbox row is removed; on failure the attempt
        count and a sanitized error are recorded so the row remains for the
        next retry pass.
        """
        pool = self._get_pool()
        rows = await pool.fetch(
            """
            SELECT org_id, amount_usdc
            FROM org_onboarding_credit_outbox
            ORDER BY created_at
            LIMIT $1
            """,
            limit,
        )

        processed = 0
        for row in rows:
            org_id = row["org_id"]
            amount_usdc = int(row["amount_usdc"])
            try:
                await self.grant_onboarding_credit(org_id, amount_usdc)
                await self.clear_onboarding_credit_outbox(org_id)
                processed += 1
            except Exception as exc:
                logger.warning("Onboarding credit outbox retry failed org_id=%s", org_id)
                try:
                    await pool.execute(
                        """
                        UPDATE org_onboarding_credit_outbox
                        SET attempts = attempts + 1,
                            last_error = $2,
                            updated_at = NOW()
                        WHERE org_id = $1
                        """,
                        org_id,
                        str(exc)[:500],
                    )
                except Exception:
                    logger.warning("Failed to record onboarding credit outbox retry state org_id=%s", org_id)
        return processed

    async def is_promotional_credit(self, org_id: str) -> bool:
        """Return whether an org still has grant-only prepaid credit.

        Any later top-up other than the exact ledger row linked from the
        immutable onboarding-grant marker—such as an admin, Stripe, on-chain,
        or refund top-up—converts the org to the normal credit path.
        """
        pool = self._get_pool()
        row = await pool.fetchrow(
            """
            SELECT EXISTS (
                SELECT 1
                FROM org_onboarding_credit_grants
                WHERE org_id = $1
            )
            AND NOT EXISTS (
                SELECT 1
                FROM org_credit_ledger
                                WHERE org_id = g.org_id
                  AND operation = 'topup'
                                    AND id <> g.ledger_entry_id
            ) AS is_promotional
                        FROM org_onboarding_credit_grants AS g
                        WHERE g.org_id = $1
            """,
            org_id,
        )
        return bool(row["is_promotional"]) if row is not None else False

    async def get_credit_history(
        self,
        org_id: str,
        operation: str | None = None,
        limit: int = 50,
        cursor: datetime | None = None,
    ) -> list[dict]:
        """Return credit ledger entries for an org (cursor paginated, newest first)."""
        pool = self._get_pool()
        params: list = [org_id, limit]
        filters = ["org_id = $1"]
        if operation is not None:
            params.append(operation)
            filters.append(f"operation = ${len(params)}")
        if cursor is not None:
            params.append(cursor)
            filters.append(f"created_at < ${len(params)}")
        where = " AND ".join(filters)
        rows = await pool.fetch(
            f"""
            SELECT id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at
            FROM org_credit_ledger
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT $2
            """,
            *params,
        )
        return [dict(r) for r in rows]
