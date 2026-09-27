# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""CDP-backed agent wallets — per-org managed USDC wallets via Coinbase Developer Platform.

Provides:
- AgentWallet model and CRUD (provision, query, deactivate)
- On-chain USDC balance reads via CDP SDK
- Immutable audit trail for all wallet lifecycle events
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from shared.db_pool import PgPool
from teardrop.config import get_settings

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

_pool: PgPool | None = None


async def init_agent_wallets_db(pool: PgPool) -> None:
    """Store the Postgres pool reference. Called during app lifespan startup."""
    global _pool
    _pool = pool
    settings = get_settings()
    if settings.agent_wallet_enabled and not settings.cdp_configured:
        logger.warning("agent_wallets: AGENT_WALLET_ENABLED=true but CDP credentials are not set — wallet operations will fail")
    logger.info("Agent wallets DB ready (enabled=%s)", settings.agent_wallet_enabled)


async def close_agent_wallets_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Agent wallets DB reference released")


def _get_pool() -> PgPool:
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
        raise RuntimeError("CDP credentials are not configured — set CDP_API_KEY_ID, CDP_API_KEY_SECRET, and CDP_WALLET_SECRET")


def _get_cdp_client():
    """Return a CdpClient context manager. CDP SDK reads credentials from env vars."""
    _require_cdp_enabled()
    from cdp import CdpClient

    return CdpClient()


def _chain_id_to_network(chain_id: int) -> str:
    """Convert EIP-155 chain ID to CDP network name."""
    network = _CHAIN_TO_NETWORK.get(chain_id)
    if network is None:
        raise ValueError(f"Unsupported chain_id {chain_id}. Supported: {sorted(_SUPPORTED_CHAIN_IDS)}")
    return network


def _cdp_account_name(org_id: str) -> str:
    """Derive a CDP-compliant account name for an org's agent wallet.

    CDP EVM account names are capped at 36 characters, alphanumeric + hyphen
    only. Teardrop ``org_id`` values are Postgres UUIDs (36 chars), so the
    naive ``f"td-{org_id}"`` scheme (39 chars) is rejected by CDP with a 400.
    Use a short deterministic hash instead — same org_id always maps to the
    same CDP account name, and collisions are astronomically unlikely.
    """
    digest = hashlib.sha256(org_id.encode("utf-8")).hexdigest()[:24]
    return f"td-{digest}"  # 27 chars total, well under CDP's 36-char cap


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
        raise ValueError(f"Unsupported chain_id {chain_id}. Supported: {sorted(_SUPPORTED_CHAIN_IDS)}")

    # Check for existing active wallet on this chain.
    existing = await _get_wallet_row(org_id, chain_id)
    if existing is not None:
        return existing

    # Provision via CDP SDK.
    cdp_account_name = _cdp_account_name(org_id)
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
        balances = await cdp.evm.list_token_balances(address=wallet.address, network=network)
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


async def deactivate_agent_wallet(org_id: str, actor_id: str, chain_id: int | None = None) -> bool:
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
        logger.error("transfer_usdc: CDP transfer failed error_type=%s", type(exc).__name__)
        raise RuntimeError("CDP transfer failed") from exc

    logger.info("transfer_usdc: success tx_hash=%s", tx_hash)
    return tx_hash


# ─── On-chain transaction verification ───────────────────────────────────────

# Public RPC endpoints for supported chains (used when no custom RPC URL is set).
_FALLBACK_RPC: dict[int, str] = {
    8453: "https://mainnet.base.org",
    84532: "https://sepolia.base.org",
}

_TX_POLL_INTERVAL = 2.0  # seconds between eth_getTransactionReceipt polls

# Circle-issued native USDC contracts (lowercase).
_USDC_CONTRACTS: dict[int, str] = {
    8453: "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    84532: "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
}
_ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_ERC20_TRANSFER_SELECTOR = "0xa9059cbb"
_TX_HASH_PATTERN = re.compile(r"^0x[0-9a-fA-F]{64}$")

UsdcTransferOutcome = Literal["confirmed", "reverted", "pending", "mismatch"]


def _rpc_url_for_chain(chain_id: int) -> str:
    rpc_url = get_settings().base_rpc_url or _FALLBACK_RPC.get(chain_id, "")
    if not rpc_url:
        raise ValueError(f"No RPC URL available for chain_id={chain_id}. Set BASE_RPC_URL for reliable transaction verification.")
    return rpc_url


async def _json_rpc(client: httpx.AsyncClient, url: str, method: str, params: list[Any]) -> Any:
    resp = await client.post(url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"RPC {method} returned an error")
    return body.get("result")


async def check_usdc_transfer(
    tx_hash: str,
    *,
    to_address: str,
    amount_usdc: int,
    chain_id: int,
) -> UsdcTransferOutcome:
    """Classify a submitted USDC transfer without polling.

    Returns ``pending`` until the receipt's block is canonical and at or below the
    ``finalized`` head. ``confirmed`` requires a USDC ``Transfer`` log paying exactly
    *amount_usdc* to *to_address*; ``reverted`` requires the reverted tx to be the
    matching USDC ``transfer`` call. Anything else is ``mismatch``.
    """
    usdc = _USDC_CONTRACTS.get(chain_id)
    if usdc is None:
        raise ValueError(f"No USDC contract known for chain_id={chain_id}")
    if not _TX_HASH_PATTERN.match(tx_hash) or not _EIP55_PATTERN_AW.match(to_address) or amount_usdc <= 0:
        return "mismatch"

    rpc_url = _rpc_url_for_chain(chain_id)
    async with httpx.AsyncClient(timeout=10.0) as client:
        receipt = await _json_rpc(client, rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if receipt is None or receipt.get("blockNumber") is None or not receipt.get("blockHash"):
            return "pending"
        finalized = await _json_rpc(client, rpc_url, "eth_getBlockByNumber", ["finalized", False])
        if finalized is None or int(receipt["blockNumber"], 16) > int(finalized["number"], 16):
            return "pending"
        block = await _json_rpc(client, rpc_url, "eth_getBlockByNumber", [receipt["blockNumber"], False])
        if block is None or str(block.get("hash", "")).lower() != str(receipt["blockHash"]).lower():
            return "pending"

        status = receipt.get("status")
        if status == "0x0":
            tx = await _json_rpc(client, rpc_url, "eth_getTransactionByHash", [tx_hash])
            expected_input = _ERC20_TRANSFER_SELECTOR + to_address[2:].lower().rjust(64, "0") + format(amount_usdc, "064x")
            if tx is not None and str(tx.get("to", "")).lower() == usdc and str(tx.get("input", "")).lower() == expected_input:
                return "reverted"
            return "mismatch"

    if status != "0x1":
        return "mismatch"
    recipient = int(to_address, 16)
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if (
            str(log.get("address", "")).lower() == usdc
            and len(topics) == 3
            and str(topics[0]).lower() == _ERC20_TRANSFER_TOPIC
            and int(topics[2], 16) == recipient
            and int(log.get("data") or "0x0", 16) == amount_usdc
        ):
            return "confirmed"
    return "mismatch"


async def verify_usdc_transfer(
    tx_hash: str,
    chain_id: int | None = None,
    timeout_seconds: int | None = None,
) -> bool:
    """Verify a transaction was mined and succeeded on-chain.

    Polls ``eth_getTransactionReceipt`` until the tx is included in a block.

    Args:
        tx_hash: Hex transaction hash (``0x...``).
        chain_id: EIP-155 chain ID. Defaults to the configured CDP network.
        timeout_seconds: Max seconds to wait for a receipt. Defaults to
            ``marketplace_tx_confirm_timeout_seconds`` from settings.

    Returns:
        ``True`` if the transaction was mined with status ``0x1`` (success).
        ``False`` if the transaction was mined with status ``0x0`` (reverted).

    Raises:
        TimeoutError: No receipt found within *timeout_seconds*.
        ValueError: Could not determine an RPC endpoint for *chain_id*.
    """
    settings = get_settings()

    if chain_id is None:
        chain_id = 84532 if settings.cdp_network == "base-sepolia" else 8453

    if timeout_seconds is None:
        timeout_seconds = settings.marketplace_tx_confirm_timeout_seconds

    rpc_url = _rpc_url_for_chain(chain_id)

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
        "id": 1,
    }
    deadline = time.monotonic() + timeout_seconds

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                resp = await client.post(rpc_url, json=payload)
                resp.raise_for_status()
                receipt = resp.json().get("result")
                if receipt is not None:
                    confirmed = receipt.get("status") == "0x1"
                    logger.info(
                        "verify_usdc_transfer: tx=%s chain=%d confirmed=%s",
                        tx_hash,
                        chain_id,
                        confirmed,
                    )
                    return confirmed
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                logger.debug("verify_usdc_transfer: poll error tx=%s error_type=%s", tx_hash, type(exc).__name__)

            if time.monotonic() >= deadline:
                raise TimeoutError(f"Transaction {tx_hash} not mined within {timeout_seconds}s on chain {chain_id}")
            await asyncio.sleep(_TX_POLL_INTERVAL)


async def get_settlement_wallet_balance_usdc(chain_id: int | None = None) -> int:
    """Return the settlement CDP account's USDC balance in atomic units.

    Used by the sweep loop to check whether the platform pool needs topping up.
    Raises ``RuntimeError`` if CDP is not enabled/configured.
    """
    _require_cdp_enabled()
    settings = get_settings()

    if chain_id is None:
        chain_id = 84532 if settings.cdp_network == "base-sepolia" else 8453

    network = _chain_id_to_network(chain_id)
    account_name = settings.marketplace_settlement_cdp_account

    async with _get_cdp_client() as cdp:
        account = await cdp.evm.get_or_create_account(name=account_name)
        balances = await cdp.evm.list_token_balances(address=account.address, network=network)
        for tb in balances:
            symbol = getattr(tb, "symbol", "") or ""
            if symbol.upper() == "USDC":
                amt = getattr(tb, "amount", None)
                if amt is not None:
                    return int(Decimal(str(amt)) * Decimal("1_000_000"))
    return 0
