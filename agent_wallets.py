# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""CDP-backed agent wallets — per-org managed USDC wallets via Coinbase Developer Platform.

Provides:
- AgentWallet model and CRUD (provision, query, deactivate)
- On-chain USDC balance reads via CDP SDK
- Immutable audit trail for all wallet lifecycle events
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from pydantic import BaseModel

from config import get_settings

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# Maps EIP-155 chain IDs to CDP network names.
_CHAIN_TO_NETWORK: dict[int, str] = {
    84532: "base-sepolia",
    8453: "base",
}

_SUPPORTED_CHAIN_IDS = frozenset(_CHAIN_TO_NETWORK.keys())

# ─── Models ───────────────────────────────────────────────────────────────────


class AgentWallet(BaseModel):
    """Public representation of a CDP-managed agent wallet."""

    id: str
    org_id: str
    address: str  # EIP-55 checksummed
    cdp_account_name: str
    chain_id: int
    wallet_type: str  # "eoa" | "smart_account"
    is_active: bool
    created_at: datetime


# ─── Database pool ────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_agent_wallets_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference. Called during app lifespan startup."""
    global _pool
    _pool = pool
    settings = get_settings()
    if settings.agent_wallet_enabled and not settings.cdp_configured:
        logger.warning(
            "agent_wallets: AGENT_WALLET_ENABLED=true but CDP credentials are not set — "
            "wallet operations will fail"
        )
    logger.info("Agent wallets DB ready (enabled=%s)", settings.agent_wallet_enabled)


async def close_agent_wallets_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Agent wallets DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Agent wallets DB not initialised — call init_agent_wallets_db() first")
    return _pool


# ─── CDP client ───────────────────────────────────────────────────────────────


def _require_cdp_enabled() -> None:
    """Raise if the feature is disabled or CDP credentials are missing."""
    settings = get_settings()
    if not settings.agent_wallet_enabled:
        raise RuntimeError("Agent wallets are disabled (AGENT_WALLET_ENABLED=false)")
    if not settings.cdp_configured:
        raise RuntimeError(
            "CDP credentials are not configured — set CDP_API_KEY_ID, "
            "CDP_API_KEY_SECRET, and CDP_WALLET_SECRET"
        )


def _get_cdp_client():
    """Return a CdpClient context manager. CDP SDK reads credentials from env vars."""
    _require_cdp_enabled()
    from cdp import CdpClient

    return CdpClient()


def _chain_id_to_network(chain_id: int) -> str:
    """Convert EIP-155 chain ID to CDP network name."""
    network = _CHAIN_TO_NETWORK.get(chain_id)
    if network is None:
        raise ValueError(
            f"Unsupported chain_id {chain_id}. Supported: {sorted(_SUPPORTED_CHAIN_IDS)}"
        )
    return network


# ─── Audit logging ────────────────────────────────────────────────────────────


async def _record_wallet_event(
    org_id: str,
    wallet_id: str,
    event_type: str,
    actor_id: str,
    amount_usdc: int = 0,
    detail: dict[str, Any] | None = None,
) -> None:
    """Insert an immutable audit event. Best-effort — errors logged, never raised."""
    try:
        pool = _get_pool()
        await pool.execute(
            "INSERT INTO agent_wallet_events "
            "(id, org_id, wallet_id, event_type, amount_usdc, detail, actor_id, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            str(uuid.uuid4()),
            org_id,
            wallet_id,
            event_type,
            amount_usdc,
            detail or {},
            actor_id,
            datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("Failed to record agent wallet event type=%s org=%s", event_type, org_id)


# ─── CRUD ─────────────────────────────────────────────────────────────────────


async def create_agent_wallet(org_id: str, actor_id: str, chain_id: int | None = None) -> AgentWallet:
    """Provision a CDP-backed agent wallet for an org. Idempotent — returns existing if found."""
    _require_cdp_enabled()
    settings = get_settings()
    pool = _get_pool()

    if chain_id is None:
        chain_id = 84532 if settings.cdp_network == "base-sepolia" else 8453

    if chain_id not in _SUPPORTED_CHAIN_IDS:
        raise ValueError(
            f"Unsupported chain_id {chain_id}. Supported: {sorted(_SUPPORTED_CHAIN_IDS)}"
        )

    # Check for existing active wallet on this chain.
    existing = await _get_wallet_row(org_id, chain_id)
    if existing is not None:
        return existing

    # Provision via CDP SDK.
    cdp_account_name = f"td-{org_id}"
    network = _chain_id_to_network(chain_id)

    async with _get_cdp_client() as cdp:
        account = await cdp.evm.get_or_create_account(name=cdp_account_name)
        address = account.address

    # EIP-55 checksum via web3.
    from web3 import Web3

    address = Web3.to_checksum_address(address)

    wallet_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # INSERT with ON CONFLICT guard for concurrent creation race.
    await pool.execute(
        "INSERT INTO org_agent_wallets "
        "(id, org_id, address, cdp_account_name, chain_id, wallet_type, is_active, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7) "
        "ON CONFLICT (org_id, chain_id) DO NOTHING",
        wallet_id,
        org_id,
        address,
        cdp_account_name,
        chain_id,
        "eoa",
        now,
    )

    # Re-read to get the canonical row (may differ from our INSERT if race lost).
    wallet = await _get_wallet_row(org_id, chain_id)
    if wallet is None:
        raise RuntimeError("Agent wallet creation failed unexpectedly")

    await _record_wallet_event(
        org_id=org_id,
        wallet_id=wallet.id,
        event_type="created",
        actor_id=actor_id,
        detail={"address": address, "chain_id": chain_id, "network": network},
    )

    logger.info("Agent wallet provisioned org=%s address=%s chain=%d", org_id, address, chain_id)
    return wallet


async def get_agent_wallet(org_id: str, chain_id: int | None = None) -> AgentWallet | None:
    """Return the active agent wallet for an org on the given chain, or None."""
    settings = get_settings()
    if chain_id is None:
        chain_id = 84532 if settings.cdp_network == "base-sepolia" else 8453
    return await _get_wallet_row(org_id, chain_id)


async def get_agent_wallet_balance(org_id: str, chain_id: int | None = None) -> dict[str, Any]:
    """Query on-chain USDC balance for the org's agent wallet via CDP SDK.

    Returns dict with balance_usdc (atomic int), address, chain_id.
    Raises ValueError if wallet not found.
    """
    _require_cdp_enabled()
    settings = get_settings()
    if chain_id is None:
        chain_id = 84532 if settings.cdp_network == "base-sepolia" else 8453

    wallet = await _get_wallet_row(org_id, chain_id)
    if wallet is None:
        raise ValueError(f"No active agent wallet for org {org_id} on chain {chain_id}")

    network = _chain_id_to_network(chain_id)
    balance_usdc = 0

    async with _get_cdp_client() as cdp:
        balances = await cdp.evm.list_token_balances(
            address=wallet.address, network=network
        )
        for token_balance in balances:
            # CDP returns token balances with symbol and amount.
            # USDC has 6 decimals — the SDK returns a Decimal or string.
            symbol = getattr(token_balance, "symbol", "") or ""
            if symbol.upper() == "USDC":
                amount = getattr(token_balance, "amount", None)
                if amount is not None:
                    # Convert to atomic units (6 decimals).
                    from decimal import Decimal

                    balance_usdc = int(Decimal(str(amount)) * Decimal("1_000_000"))
                break

    return {
        "balance_usdc": balance_usdc,
        "address": wallet.address,
        "chain_id": wallet.chain_id,
    }


async def deactivate_agent_wallet(
    org_id: str, actor_id: str, chain_id: int | None = None
) -> bool:
    """Soft-deactivate the org's agent wallet. Returns True if a wallet was deactivated."""
    settings = get_settings()
    pool = _get_pool()
    if chain_id is None:
        chain_id = 84532 if settings.cdp_network == "base-sepolia" else 8453

    wallet = await _get_wallet_row(org_id, chain_id)
    if wallet is None:
        return False

    await pool.execute(
        "UPDATE org_agent_wallets SET is_active = FALSE WHERE id = $1",
        wallet.id,
    )

    await _record_wallet_event(
        org_id=org_id,
        wallet_id=wallet.id,
        event_type="deactivated",
        actor_id=actor_id,
    )

    logger.info("Agent wallet deactivated org=%s wallet=%s", org_id, wallet.id)
    return True


# ─── Internal helpers ─────────────────────────────────────────────────────────


async def _get_wallet_row(org_id: str, chain_id: int) -> AgentWallet | None:
    """Fetch a single active wallet row from Postgres."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, org_id, address, cdp_account_name, chain_id, wallet_type, is_active, created_at "
        "FROM org_agent_wallets "
        "WHERE org_id = $1 AND chain_id = $2 AND is_active = TRUE",
        org_id,
        chain_id,
    )
    if row is None:
        return None
    return AgentWallet(
        id=row["id"],
        org_id=row["org_id"],
        address=row["address"],
        cdp_account_name=row["cdp_account_name"],
        chain_id=row["chain_id"],
        wallet_type=row["wallet_type"],
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


# ─── USDC transfer ───────────────────────────────────────────────────────────

_EIP55_PATTERN_AW = __import__("re").compile(r"^0x[0-9a-fA-F]{40}$")


async def transfer_usdc(
    from_cdp_account: str,
    to_address: str,
    amount_usdc: int,
    chain_id: int | None = None,
) -> str:
    """Transfer USDC from a CDP-managed account to an external address.

    Args:
        from_cdp_account: CDP account name (e.g. ``"td-marketplace"``).
        to_address: Destination EIP-55 wallet address.
        amount_usdc: Amount in atomic USDC (6 decimals; 1_000_000 = $1.00).
        chain_id: EIP-155 chain ID. Defaults to the configured CDP network.

    Returns:
        Transaction hash string.

    Raises:
        ValueError: Invalid address or amount.
        RuntimeError: CDP transfer failure.
    """
    _require_cdp_enabled()
    settings = get_settings()

    if chain_id is None:
        chain_id = 84532 if settings.cdp_network == "base-sepolia" else 8453

    if not _EIP55_PATTERN_AW.match(to_address):
        raise ValueError(f"Invalid destination address: {to_address}")
    if amount_usdc <= 0:
        raise ValueError(f"Transfer amount must be positive, got {amount_usdc}")

    network = _chain_id_to_network(chain_id)
    # CDP SDK uses human-readable USDC amounts (e.g. "1.50" for $1.50).
    from decimal import Decimal

    human_amount = str(Decimal(amount_usdc) / Decimal("1_000_000"))

    logger.info(
        "transfer_usdc: %s USDC from account=%s to=%s network=%s",
        human_amount,
        from_cdp_account,
        to_address,
        network,
    )

    try:
        async with _get_cdp_client() as cdp:
            result = await cdp.evm.transfer(
                from_account=from_cdp_account,
                to=to_address,
                token="usdc",
                amount=human_amount,
                network=network,
            )
            tx_hash = getattr(result, "transaction_hash", None) or str(result)
    except Exception as exc:
        logger.error("transfer_usdc: CDP transfer failed: %s", exc)
        raise RuntimeError(f"CDP transfer failed: {exc}") from exc

    logger.info("transfer_usdc: success tx_hash=%s", tx_hash)
    return tx_hash
