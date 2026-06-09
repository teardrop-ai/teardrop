# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace package facade with compatibility state bridging.

Owner map (which submodule owns which concern):
  - catalog.py:       tool catalog CRUD, lookup by name/slug, publish state
  - subscriptions.py: org subscriptions to marketplace tools
  - earnings.py:      author earnings accrual per tool call
  - worker.py:        background withdrawal sweep loop
  - withdrawals.py:   withdrawal lifecycle (pending/process/complete/reset)
  - stats.py:         marketplace usage & revenue statistics
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

import asyncpg

import marketplace.catalog as _catalog
import marketplace.context as _ctx
import marketplace.earnings as _earnings
import marketplace.stats as _stats
import marketplace.subscriptions as _subscriptions
import marketplace.withdrawals as _withdrawals
import marketplace.worker as _worker
from marketplace.models import (
    AuthorConfig,
    AuthorEarning,
    AuthorEarningByTool,
    AuthorWithdrawal,
    MarketplaceSubscription,
    MarketplaceTool,
    validate_eip55_address,
)
from teardrop.config import get_settings

T = TypeVar("T")

# Preserve originals so wrappers can safely redirect module globals.
_INIT_MARKETPLACE_DB_ORIG = _ctx.init_marketplace_db
_CLOSE_MARKETPLACE_DB_ORIG = _ctx.close_marketplace_db
_GET_POOL_ORIG = _ctx._get_pool

_SET_AUTHOR_CONFIG_ORIG = _catalog.set_author_config
_GET_AUTHOR_CONFIG_ORIG = _catalog.get_author_config
_GET_MARKETPLACE_CATALOG_ORIG = _catalog.get_marketplace_catalog
_GET_MARKETPLACE_CATALOG_TOOL_ORIG = _catalog.get_marketplace_catalog_tool
_GET_MARKETPLACE_AUTHOR_SUMMARY_ORIG = _catalog.get_marketplace_author_summary
_BUILD_CATALOG_CURSOR_ORIG = _catalog._build_catalog_cursor
_GET_MARKETPLACE_TOOL_BY_NAME_ORIG = _catalog.get_marketplace_tool_by_name
_GET_PLATFORM_TOOL_CACHE_ORIG = _catalog._get_platform_tool_cache
_GET_ORG_TOOL_PRICE_CACHE_ORIG = _catalog._get_org_tool_price_cache
_INVALIDATE_PLATFORM_TOOL_CACHE_ORIG = _catalog._invalidate_platform_tool_cache
_INVALIDATE_ALL_ORG_TOOL_PRICE_CACHE_ORIG = _catalog._invalidate_all_org_tool_price_cache
_GET_PLATFORM_TOOL_PRICE_ORIG = _catalog.get_platform_tool_price
_GET_ORG_TOOL_PRICE_BY_QUALIFIED_NAME_ORIG = _catalog.get_org_tool_price_by_qualified_name

_RECORD_TOOL_CALL_EARNINGS_ORIG = _earnings.record_tool_call_earnings
_GET_AUTHOR_BALANCE_ORIG = _earnings.get_author_balance
_GET_AUTHOR_EARNINGS_HISTORY_ORIG = _earnings.get_author_earnings_history
_GET_AUTHOR_EARNINGS_BY_TOOL_ORIG = _earnings.get_author_earnings_by_tool

_RECORD_MARKETPLACE_TOOL_CALL_ORIG = _stats.record_marketplace_tool_call
_RECORD_MARKETPLACE_TOOL_USAGE_ORIG = _stats.record_marketplace_tool_usage
_RECORD_MARKETPLACE_TOOL_USAGE_MANY_ORIG = _stats.record_marketplace_tool_usage_many

_SUBSCRIBE_TO_TOOL_ORIG = _subscriptions.subscribe_to_tool
_UNSUBSCRIBE_FROM_TOOL_ORIG = _subscriptions.unsubscribe_from_tool
_GET_ORG_SUBSCRIPTIONS_ORIG = _subscriptions.get_org_subscriptions
_GET_SUBSCRIBED_TOOLS_CATALOG_ORIG = _subscriptions.get_subscribed_tools_catalog
_CHECK_ORG_SUBSCRIPTION_ORIG = _subscriptions.check_org_subscription
_BUILD_SUBSCRIBED_MARKETPLACE_TOOLS_ORIG = _subscriptions.build_subscribed_marketplace_tools
_BUILD_MARKETPLACE_LANGCHAIN_TOOL_ORIG = _subscriptions._build_marketplace_langchain_tool
_INVALIDATE_SUBSCRIPTION_CACHE_ORIG = _subscriptions._invalidate_subscription_cache

_REQUEST_WITHDRAWAL_ORIG = _withdrawals.request_withdrawal
_PROCESS_WITHDRAWAL_ORIG = _withdrawals.process_withdrawal
_COMPLETE_WITHDRAWAL_ORIG = _withdrawals.complete_withdrawal
_LIST_PENDING_WITHDRAWALS_ORIG = _withdrawals.list_pending_withdrawals
_LIST_ORG_WITHDRAWALS_ORIG = _withdrawals.list_org_withdrawals
_RESET_WITHDRAWAL_ORIG = _withdrawals.reset_withdrawal
_LIST_EXHAUSTED_WITHDRAWALS_ORIG = _withdrawals.list_exhausted_withdrawals
_GET_WITHDRAWAL_SERVICE_ORIG = _withdrawals._get_withdrawal_service
_NOTIFY_SUBSCRIBERS_OF_DEACTIVATION_ORIG = _withdrawals.notify_subscribers_of_deactivation
_AUTO_DEACTIVATE_TOOL_FOR_HEALTH_ORIG = _withdrawals.auto_deactivate_tool_for_health

_SWEEP_WITHDRAWAL_ID_ORIG = _worker._sweep_withdrawal_id
_SWEEP_BACKOFF_SECONDS_ORIG = _worker._sweep_backoff_seconds
_MARKETPLACE_SWEEP_ONCE_ORIG = _worker.marketplace_sweep_once
_MARKETPLACE_SWEEP_LOOP_ORIG = _worker._marketplace_sweep_loop

# Root-level mutable compatibility state patched by tests.
_pool: asyncpg.Pool | None = _ctx._pool
_SUBSCRIPTION_CACHE = _subscriptions._SUBSCRIPTION_CACHE
PLATFORM_SLUG = _catalog.PLATFORM_SLUG
PlatformToolSubscriptionError = _subscriptions.PlatformToolSubscriptionError
SelfSubscribeError = _subscriptions.SelfSubscribeError


def _sync_to_modules() -> None:
    """Push root compatibility state and monkeypatch hooks into submodules."""
    _ctx._pool = _pool
    _catalog._get_pool = _get_pool
    _earnings._get_pool = _get_pool
    _stats._get_pool = _get_pool
    _subscriptions._get_pool = _get_pool
    _withdrawals._get_pool = _get_pool
    _worker._get_pool = _get_pool

    _earnings.get_settings = get_settings
    _subscriptions.get_settings = get_settings
    _withdrawals.get_settings = get_settings
    _worker.get_settings = get_settings
    _worker.asyncio = asyncio

    _subscriptions.get_marketplace_tool_by_name = get_marketplace_tool_by_name
    _subscriptions.get_org_subscriptions = get_org_subscriptions

    _stats.get_marketplace_tool_by_name = get_marketplace_tool_by_name
    _stats.get_platform_tool_price = get_platform_tool_price

    _earnings.get_author_config = get_author_config
    _withdrawals.get_author_config = get_author_config
    _withdrawals.get_author_balance = get_author_balance
    _worker.get_author_config = get_author_config
    _worker.process_withdrawal = process_withdrawal
    _worker.marketplace_sweep_once = marketplace_sweep_once


def _sync_from_modules() -> None:
    """Pull submodule state back to root compatibility symbols."""
    global _pool
    _pool = _ctx._pool


async def _call_async(func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    _sync_to_modules()
    try:
        return await func(*args, **kwargs)
    finally:
        _sync_from_modules()


def _call_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    _sync_to_modules()
    try:
        return func(*args, **kwargs)
    finally:
        _sync_from_modules()


async def init_marketplace_db(pool: asyncpg.Pool) -> None:
    """Initialise marketplace tables and caches at application startup."""
    await _call_async(_INIT_MARKETPLACE_DB_ORIG, pool)


async def close_marketplace_db() -> None:
    """Release marketplace DB/cache resources at shutdown."""
    await _call_async(_CLOSE_MARKETPLACE_DB_ORIG)


def _get_pool() -> asyncpg.Pool:
    return _call_sync(_GET_POOL_ORIG)


async def set_author_config(org_id: str, *, settlement_wallet: str) -> AuthorConfig:
    """Create/update an author's payout config (EIP-55 ``settlement_wallet`` for USDC withdrawals)."""
    return await _call_async(_SET_AUTHOR_CONFIG_ORIG, org_id, settlement_wallet=settlement_wallet)


async def get_author_config(org_id: str) -> AuthorConfig | None:
    """Return the author's payout config (settlement wallet), or None if unconfigured."""
    return await _call_async(_GET_AUTHOR_CONFIG_ORIG, org_id)


async def get_marketplace_catalog(
    tool_overrides: dict[str, int] | None = None,
    default_tool_cost: int = 0,
    *,
    org_slug: str | None = None,
    category: str | None = None,
    sort: str = "name",
    limit: int = 100,
    cursor: str | None = None,
    tool_name: str | None = None,
    q: str | None = None,
) -> list[MarketplaceTool]:
    """Return published catalog tools (community + platform) with effective per-call prices.

    Tool cost precedence applied per entry: per-org override -> marketplace base price ->
    ``default_tool_cost``. Supports org_slug/category/search filtering, sorting, and keyset pagination.
    """
    return await _call_async(
        _GET_MARKETPLACE_CATALOG_ORIG,
        tool_overrides,
        default_tool_cost,
        org_slug=org_slug,
        category=category,
        sort=sort,
        limit=limit,
        cursor=cursor,
        tool_name=tool_name,
        q=q,
    )


async def get_marketplace_catalog_tool(
    tool_name: str,
    org_slug: str,
    tool_overrides: dict[str, int] | None = None,
    default_tool_cost: int = 0,
) -> MarketplaceTool | None:
    """Return a single published catalog tool by ``org_slug``/``tool_name`` with its effective price."""
    return await _call_async(
        _GET_MARKETPLACE_CATALOG_TOOL_ORIG,
        tool_name,
        org_slug,
        tool_overrides,
        default_tool_cost,
    )


async def get_marketplace_author_summary(org_slug: str) -> dict[str, Any] | None:
    """Return a public author profile (org name, tool count, aggregate call stats) by slug."""
    return await _call_async(_GET_MARKETPLACE_AUTHOR_SUMMARY_ORIG, org_slug)


def _build_catalog_cursor(tool: MarketplaceTool, sort: str) -> str:
    return _call_sync(_BUILD_CATALOG_CURSOR_ORIG, tool, sort)


async def get_marketplace_tool_by_name(tool_name: str, org_slug: str) -> dict[str, Any] | None:
    """Return the raw published tool row by ``org_slug``/``tool_name`` (None if not found)."""
    return await _call_async(_GET_MARKETPLACE_TOOL_BY_NAME_ORIG, tool_name, org_slug)


def _get_platform_tool_cache(tool_name: str):
    return _call_sync(_GET_PLATFORM_TOOL_CACHE_ORIG, tool_name)


def _get_org_tool_price_cache(qualified_name: str):
    return _call_sync(_GET_ORG_TOOL_PRICE_CACHE_ORIG, qualified_name)


async def _invalidate_platform_tool_cache() -> None:
    await _call_async(_INVALIDATE_PLATFORM_TOOL_CACHE_ORIG)


async def _invalidate_all_org_tool_price_cache() -> None:
    await _call_async(_INVALIDATE_ALL_ORG_TOOL_PRICE_CACHE_ORIG)


async def get_platform_tool_price(tool_name: str) -> int | None:
    """Return the platform (built-in) tool's per-call price in atomic USDC, or None."""
    return await _call_async(_GET_PLATFORM_TOOL_PRICE_ORIG, tool_name)


async def get_org_tool_price_by_qualified_name(qualified_name: str) -> int | None:
    """Return a community tool's per-call price (atomic USDC) by ``org_slug/tool_name``."""
    return await _call_async(_GET_ORG_TOOL_PRICE_BY_QUALIFIED_NAME_ORIG, qualified_name)


async def record_tool_call_earnings(
    author_org_id: str,
    tool_name: str,
    caller_org_id: str,
    total_cost_usdc: int,
) -> None:
    """Record immutable author earnings for one billable tool call.

    Splits ``total_cost_usdc`` by ``revenue_share_bps`` (default 70% author / 30% platform)
    into the author earnings ledger. Financial mutation — append-only.
    """
    await _call_async(_RECORD_TOOL_CALL_EARNINGS_ORIG, author_org_id, tool_name, caller_org_id, total_cost_usdc)


async def get_author_balance(org_id: str) -> int:
    """Return the author's withdrawable earnings balance in atomic USDC."""
    return await _call_async(_GET_AUTHOR_BALANCE_ORIG, org_id)


async def get_author_earnings_history(
    org_id: str,
    limit: int = 50,
    cursor=None,
    tool_name: str | None = None,
) -> tuple[list[AuthorEarning], str | None]:
    """Return cursor-paginated author earnings entries, optionally filtered by tool."""
    return await _call_async(_GET_AUTHOR_EARNINGS_HISTORY_ORIG, org_id, limit, cursor, tool_name)


async def get_author_earnings_by_tool(org_id: str) -> list[AuthorEarningByTool]:
    """Return per-tool earnings rollups (total/pending/settled author share) for an org."""
    return await _call_async(_GET_AUTHOR_EARNINGS_BY_TOOL_ORIG, org_id)


async def record_marketplace_tool_call(
    qualified_tool_name: str,
    *,
    tool_type: str,
    author_org_id: str | None = None,
    increment: int = 1,
) -> None:
    """Increment the public ``total_calls`` analytics counter for one tool (non-financial)."""
    await _call_async(
        _RECORD_MARKETPLACE_TOOL_CALL_ORIG,
        qualified_tool_name,
        tool_type=tool_type,
        author_org_id=author_org_id,
        increment=increment,
    )


async def record_marketplace_tool_usage(tool_name: str) -> bool:
    """Increment the usage counter for a single marketplace tool; returns True if recorded."""
    return await _call_async(_RECORD_MARKETPLACE_TOOL_USAGE_ORIG, tool_name)


async def record_marketplace_tool_usage_many(tool_names: list[str]) -> None:
    """Batch-increment the ``total_calls`` analytics counter for many tools in one round-trip."""
    await _call_async(_RECORD_MARKETPLACE_TOOL_USAGE_MANY_ORIG, tool_names)


async def subscribe_to_tool(org_id: str, qualified_tool_name: str) -> MarketplaceSubscription:
    """Subscribe an org to a community tool (``org_slug/tool_name``), enabling billed calls."""
    return await _call_async(_SUBSCRIBE_TO_TOOL_ORIG, org_id, qualified_tool_name)


async def unsubscribe_from_tool(subscription_id: str, org_id: str) -> bool:
    """Cancel an org's tool subscription; returns True if a subscription was removed."""
    return await _call_async(_UNSUBSCRIBE_FROM_TOOL_ORIG, subscription_id, org_id)


async def get_org_subscriptions(org_id: str) -> list[MarketplaceSubscription]:
    """Return the org's active marketplace tool subscriptions."""
    return await _call_async(_GET_ORG_SUBSCRIPTIONS_ORIG, org_id)


async def get_subscribed_tools_catalog(
    org_id: str,
    tool_overrides: dict[str, int] | None = None,
    default_tool_cost: int = 0,
) -> list[MarketplaceTool]:
    """Return catalog entries for the org's subscribed tools with effective per-call prices."""
    return await _call_async(_GET_SUBSCRIBED_TOOLS_CATALOG_ORIG, org_id, tool_overrides, default_tool_cost)


async def check_org_subscription(org_id: str, qualified_tool_name: str) -> bool:
    """Return True if the org has an active subscription to the given community tool."""
    return await _call_async(_CHECK_ORG_SUBSCRIPTION_ORIG, org_id, qualified_tool_name)


async def build_subscribed_marketplace_tools(org_id: str) -> tuple[list, dict[str, Any]]:
    """Build LangChain tool objects (plus price metadata) for the org's subscribed tools."""
    return await _call_async(_BUILD_SUBSCRIBED_MARKETPLACE_TOOLS_ORIG, org_id)


def _build_marketplace_langchain_tool(tool_row: dict[str, Any], qualified_name: str):
    return _call_sync(_BUILD_MARKETPLACE_LANGCHAIN_TOOL_ORIG, tool_row, qualified_name)


def _invalidate_subscription_cache(org_id: str) -> None:
    _call_sync(_INVALIDATE_SUBSCRIPTION_CACHE_ORIG, org_id)


async def request_withdrawal(org_id: str, amount_usdc: int) -> AuthorWithdrawal:
    """Request a payout of ``amount_usdc`` (atomic) from the author's earnings balance.

    Atomically debits the balance and creates a pending withdrawal. If the downstream
    CDP on-chain transfer fails, the withdrawal is reverted to pending so funds are
    never lost. Financial mutation — validates against the available balance.
    """
    return await _call_async(_REQUEST_WITHDRAWAL_ORIG, org_id, amount_usdc)


async def process_withdrawal(withdrawal_id: str) -> AuthorWithdrawal:
    """Execute the on-chain CDP USDC transfer for a pending withdrawal."""
    return await _call_async(_PROCESS_WITHDRAWAL_ORIG, withdrawal_id)


async def complete_withdrawal(withdrawal_id: str, tx_hash: str) -> None:
    """Mark a withdrawal settled and record its on-chain ``tx_hash``."""
    await _call_async(_COMPLETE_WITHDRAWAL_ORIG, withdrawal_id, tx_hash)


async def list_pending_withdrawals(org_id: str | None = None) -> list[AuthorWithdrawal]:
    """Return pending withdrawals, optionally scoped to one org."""
    return await _call_async(_LIST_PENDING_WITHDRAWALS_ORIG, org_id)


async def list_org_withdrawals(
    org_id: str,
    limit: int = 50,
    cursor=None,
) -> tuple[list[AuthorWithdrawal], str | None]:
    """Return cursor-paginated withdrawal history for an org."""
    return await _call_async(_LIST_ORG_WITHDRAWALS_ORIG, org_id, limit, cursor)


async def reset_withdrawal(withdrawal_id: str) -> bool:
    """Re-arm a retry-exhausted withdrawal for another processing attempt."""
    return await _call_async(_RESET_WITHDRAWAL_ORIG, withdrawal_id)


async def list_exhausted_withdrawals(limit: int = 50) -> list[AuthorWithdrawal]:
    """Return withdrawals that have exhausted their on-chain retry budget."""
    return await _call_async(_LIST_EXHAUSTED_WITHDRAWALS_ORIG, limit)


def _get_withdrawal_service():
    return _call_sync(_GET_WITHDRAWAL_SERVICE_ORIG)


async def notify_subscribers_of_deactivation(qualified_tool_name: str, reason: str) -> None:
    """Email subscribers when a community tool is deactivated, with the given reason."""
    await _call_async(_NOTIFY_SUBSCRIBERS_OF_DEACTIVATION_ORIG, qualified_tool_name, reason)


async def auto_deactivate_tool_for_health(tool_id: str, qualified_tool_name: str | None = None) -> None:
    """Auto-deactivate a community tool that breached health/error thresholds and notify subscribers."""
    await _call_async(_AUTO_DEACTIVATE_TOOL_FOR_HEALTH_ORIG, tool_id, qualified_tool_name)


def _sweep_withdrawal_id(org_id: str, epoch_hour: int) -> str:
    return _call_sync(_SWEEP_WITHDRAWAL_ID_ORIG, org_id, epoch_hour)


def _sweep_backoff_seconds(attempt: int) -> int:
    return _call_sync(_SWEEP_BACKOFF_SECONDS_ORIG, attempt)


async def marketplace_sweep_once() -> int:
    """Run one settlement-sweep pass over pending author withdrawals.

    Processes due withdrawals with per-org idempotency (``_sweep_withdrawal_id``) and
    exponential backoff; a circuit breaker halts the sweep after repeated failures so a
    failing CDP backend cannot drain retries. Returns the number of withdrawals processed.
    """
    return await _call_async(_MARKETPLACE_SWEEP_ONCE_ORIG)


async def _marketplace_sweep_loop() -> None:
    await _call_async(_MARKETPLACE_SWEEP_LOOP_ORIG)


__all__ = [
    "AuthorConfig",
    "AuthorEarning",
    "AuthorEarningByTool",
    "AuthorWithdrawal",
    "MarketplaceTool",
    "MarketplaceSubscription",
    "validate_eip55_address",
    "init_marketplace_db",
    "close_marketplace_db",
    "_pool",
    "_get_pool",
    "get_settings",
    "set_author_config",
    "get_author_config",
    "get_marketplace_catalog",
    "get_marketplace_catalog_tool",
    "get_marketplace_author_summary",
    "_build_catalog_cursor",
    "get_marketplace_tool_by_name",
    "_get_platform_tool_cache",
    "_get_org_tool_price_cache",
    "_invalidate_platform_tool_cache",
    "_invalidate_all_org_tool_price_cache",
    "get_platform_tool_price",
    "get_org_tool_price_by_qualified_name",
    "record_tool_call_earnings",
    "get_author_balance",
    "get_author_earnings_history",
    "get_author_earnings_by_tool",
    "record_marketplace_tool_call",
    "record_marketplace_tool_usage",
    "record_marketplace_tool_usage_many",
    "subscribe_to_tool",
    "unsubscribe_from_tool",
    "get_org_subscriptions",
    "get_subscribed_tools_catalog",
    "_SUBSCRIPTION_CACHE",
    "_invalidate_subscription_cache",
    "check_org_subscription",
    "build_subscribed_marketplace_tools",
    "_build_marketplace_langchain_tool",
    "request_withdrawal",
    "process_withdrawal",
    "complete_withdrawal",
    "list_pending_withdrawals",
    "list_org_withdrawals",
    "reset_withdrawal",
    "list_exhausted_withdrawals",
    "_get_withdrawal_service",
    "notify_subscribers_of_deactivation",
    "auto_deactivate_tool_for_health",
    "_sweep_withdrawal_id",
    "_sweep_backoff_seconds",
    "marketplace_sweep_once",
    "_marketplace_sweep_loop",
]
