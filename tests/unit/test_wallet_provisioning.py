# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for collision-free wallet provisioning (teardrop.wallets)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from teardrop.users.models import Org, User
from teardrop.wallets import Wallet, WalletProvisioningResult, provision_org_for_wallet


def _make_wallet(address: str, org_id: str = "org-existing", user_id: str = "user-existing") -> Wallet:
    return Wallet(
        id="wallet-1",
        address=address,
        chain_id=1,
        user_id=user_id,
        org_id=org_id,
        is_primary=True,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def _captured(monkeypatch):
    """Capture SQL executed inside the provisioning transaction."""
    calls: list[tuple[str, tuple]] = []

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=lambda sql, *args: calls.append((sql, args)) or "INSERT 0 1")
    conn.fetchrow = AsyncMock(
        side_effect=lambda sql, *args: (
            {
                "id": "org-created",
                "name": args[1],
                "slug": args[2],
                "acquisition_source": args[3],
                "created_at": args[4],
            }
            if "INSERT INTO orgs" in sql and "RETURNING" in sql
            else {"id": "wallet-1"}
            if "RETURNING id" in sql and "wallets" in sql
            else None
        )
    )
    conn.transaction = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)))

    pool = MagicMock()
    pool.acquire = MagicMock(
        return_value=MagicMock(__aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock(return_value=False))
    )

    monkeypatch.setattr("teardrop.wallets._pool", pool)
    monkeypatch.setattr(
        "teardrop.wallets.get_settings",
        lambda: type("S", (), {"machine_org_daily_spend_limit_usdc": 5_000_000})(),
    )
    return {"calls": calls, "conn": conn, "pool": pool}


class TestProvisionOrgForWallet:
    @pytest.mark.anyio
    async def test_happy_path_creates_full_address_org(self, _captured):
        result = await provision_org_for_wallet("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", 1, acquisition_source="siwe")

        assert isinstance(result, WalletProvisioningResult)
        assert result.created is True
        # Full 42-char address must appear in the org name — no short prefixes.
        assert result.org.name == "wallet-0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
        assert len(result.org.name) == len("wallet-") + 42
        assert result.org.acquisition_source == "siwe"
        assert result.user.email == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045@wallet"
        assert result.user.role == "user"

    @pytest.mark.anyio
    async def test_org_credits_row_created_with_spend_cap(self, _captured):
        await provision_org_for_wallet("0xAbCdEf0123456789AbCdEf0123456789AbCdEf01", 84532)

        credit_calls = [c for c in _captured["calls"] if "org_credits" in c[0]]
        assert len(credit_calls) == 1
        assert "spending_limit_usdc" in credit_calls[0][0]
        assert "ON CONFLICT (org_id) DO NOTHING" in credit_calls[0][0]
        assert "LEAST(" not in credit_calls[0][0]
        # Cap value passed as parameter ($2 after org_id).
        assert credit_calls[0][1][1] == 5_000_000

    @pytest.mark.anyio
    async def test_race_lost_returns_existing_without_orphans(self, _captured, monkeypatch):
        """When wallets INSERT ... RETURNING yields no row, we roll back and re-read."""
        existing = _make_wallet("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
        existing_org = Org(id="org-existing", name="wallet-x", slug="wallet-x", created_at=datetime.now(timezone.utc))
        existing_user = User(
            id="user-existing",
            email="x@wallet",
            org_id="org-existing",
            hashed_secret="h",
            salt="s",
            role="user",
            is_active=True,
            is_verified=True,
            created_at=datetime.now(timezone.utc),
        )

        async def fetchrow_side_effect(sql, *args):
            if "INSERT INTO orgs" in sql and "RETURNING" in sql:
                return {
                    "id": "org-created",
                    "name": args[1],
                    "slug": args[2],
                    "acquisition_source": args[3],
                    "created_at": args[4],
                }
            if "RETURNING id" in sql and "wallets" in sql:
                return None  # race lost
            if "FROM wallets WHERE" in sql:
                return {
                    "id": existing.id,
                    "address": existing.address,
                    "chain_id": existing.chain_id,
                    "user_id": existing.user_id,
                    "org_id": existing.org_id,
                    "is_primary": True,
                    "created_at": existing.created_at,
                }
            if "FROM orgs WHERE" in sql:
                return {
                    "id": existing_org.id,
                    "name": existing_org.name,
                    "slug": existing_org.slug,
                    "acquisition_source": "siwe",
                    "created_at": existing_org.created_at,
                }
            if "FROM users WHERE" in sql:
                return {
                    "id": existing_user.id,
                    "email": existing_user.email,
                    "org_id": existing_user.org_id,
                    "hashed_secret": "h",
                    "salt": "s",
                    "role": "user",
                    "is_active": True,
                    "is_verified": True,
                    "created_at": existing_user.created_at,
                }
            return None

        _captured["conn"].fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
        _captured["pool"].fetchrow = AsyncMock(side_effect=fetchrow_side_effect)

        result = await provision_org_for_wallet("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", 1)

        assert result.created is False
        assert result.org.id == "org-existing"
        assert result.user.id == "user-existing"
        assert result.wallet.address == existing.address

    @pytest.mark.anyio
    async def test_race_lost_and_no_existing_wallet_raises(self, _captured, monkeypatch):
        monkeypatch.setattr("teardrop.wallets.get_wallet_by_address", AsyncMock(return_value=None))
        _captured["conn"].fetchrow = AsyncMock(return_value=None)

        with pytest.raises(Exception):
            await provision_org_for_wallet("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", 1)

    @pytest.mark.anyio
    async def test_distinct_addresses_get_distinct_org_names(self, _captured):
        r1 = await provision_org_for_wallet("0xAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa", 1)
        r2 = await provision_org_for_wallet("0xBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBb", 1)

        assert r1.org.name != r2.org.name

    @pytest.mark.anyio
    async def test_human_name_collision_uses_unique_machine_org_name(self, _captured):
        address = "0xAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa"
        human_org = {
            "id": "human-org",
            "name": f"wallet-{address.lower()}",
            "slug": f"wallet-{address.lower()}",
            "acquisition_source": "email",
            "created_at": datetime.now(timezone.utc),
        }
        created_org = {
            "id": "machine-org",
            "name": f"wallet-{address.lower()}-random",
            "slug": "machine-slug",
            "acquisition_source": "siwe",
            "created_at": datetime.now(timezone.utc),
        }
        org_selects = 0
        org_insert_names: list[str] = []

        async def fetchrow_side_effect(sql, *args):
            nonlocal org_selects
            if "FROM wallets" in sql:
                return None
            if "FROM orgs WHERE name" in sql:
                org_selects += 1
                return human_org if org_selects == 1 else None
            if "INSERT INTO orgs" in sql and "RETURNING" in sql:
                org_insert_names.append(args[1])
                return created_org
            if "FROM users WHERE org_id" in sql:
                return None
            if "INSERT INTO wallets" in sql:
                return {"id": "wallet-new"}
            return None

        _captured["conn"].fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
        result = await provision_org_for_wallet(address, 1)

        assert result.org.id == "machine-org"
        assert result.org.name != human_org["name"]
        assert org_insert_names[0].startswith("wallet-")
        assert org_insert_names[0].split("-")[1] != address.lower()[2:]
