# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Internal FastAPI lifespan construction helpers."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import asyncpg
from fastapi import FastAPI

from agent.graph import close_checkpointer, get_graph, init_checkpointer
from billing import close_billing, init_billing
from marketplace import close_marketplace_db, init_marketplace_db
from mcp_client import close_mcp_client_db, init_mcp_client_db
from org_tools import close_org_tools_db, init_org_tools_db
from scheduling import close_scheduling_db, init_scheduling_db, scheduled_runs_tick
from scripts.generate_keys import generate_keypair
from teardrop._background_tasks import (
    _memory_cleanup_loop,
    _onboarding_credit_outbox_loop,
    _prewarm_cache_prefixes,
    _refresh_token_cleanup_loop,
    _reputation_rollup_loop,
    _run_periodic,
    _settlement_retry_loop,
    _x402_nonce_cleanup_loop,
)
from teardrop.agent_wallets import close_agent_wallets_db, init_agent_wallets_db
from teardrop.benchmarks import close_benchmarks_db, init_benchmarks_db
from teardrop.cache import close_redis, init_redis
from teardrop.config import Settings, get_settings
from teardrop.llm_config import close_llm_config_db, init_llm_config_db
from teardrop.memory import close_memory_db, init_memory_db
from teardrop.tool_exclusions import close_tool_exclusions_db, init_tool_exclusions_db
from teardrop.usage import close_usage_db, init_usage_db
from teardrop.users import close_user_db, init_user_db
from teardrop.wallets import close_wallets_db, init_wallets_db
from tools._internals._rpc_semaphore import init_chain_rate_limiter, init_chain_semaphore, init_rpc_semaphore

settings = get_settings()
logger = logging.getLogger(__name__)


def build_lifespan(validate_production_config: Callable[[Settings], None]):
    """Build the FastAPI lifespan context manager without importing teardrop.app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from migrations.runner import apply_pending

        generate_keypair(Path(__file__).resolve().parent.parent / "keys")
        validate_production_config(settings)

        async def _init_conn(conn: asyncpg.Connection) -> None:
            try:
                from pgvector.asyncpg import register_vector

                await register_vector(conn)
            except Exception:
                pass

        pool = await asyncpg.create_pool(
            settings.pg_dsn,
            init=_init_conn,
            command_timeout=settings.pg_command_timeout,
            min_size=settings.pg_pool_min_size,
            max_size=settings.pg_pool_max_size,
        )
        logger.info(
            "Postgres pool configured: min_size=%d max_size=%d command_timeout=%.1fs",
            settings.pg_pool_min_size,
            settings.pg_pool_max_size,
            settings.pg_command_timeout,
        )
        app.state.pool = pool
        await apply_pending(pool)
        await init_redis(settings.redis_url)
        await init_checkpointer()
        await get_graph()
        await init_user_db(pool)
        await init_usage_db(pool)
        await init_wallets_db(pool)
        await init_billing(pool)
        await init_org_tools_db(pool)
        await init_mcp_client_db(pool)
        await init_memory_db(pool)
        await init_marketplace_db(pool)
        await init_llm_config_db(pool)
        await init_benchmarks_db(pool)
        await init_agent_wallets_db(pool)
        await init_scheduling_db(pool)
        await init_tool_exclusions_db(pool)

        init_rpc_semaphore(settings.agent_rpc_semaphore_limit)
        init_chain_semaphore(1, settings.agent_rpc_chain_semaphore_limit)
        init_chain_semaphore(8453, settings.agent_rpc_chain_semaphore_limit)
        init_chain_rate_limiter(1, settings.agent_rpc_chain_rps_limit)
        init_chain_rate_limiter(8453, settings.agent_rpc_chain_rps_limit)

        bg_tasks: list[asyncio.Task] = []
        if settings.billing_enabled:
            bg_tasks.append(asyncio.create_task(_settlement_retry_loop()))
            bg_tasks.append(asyncio.create_task(_x402_nonce_cleanup_loop()))
        if settings.billing_enabled and settings.onboarding_credit_enabled:
            bg_tasks.append(asyncio.create_task(_onboarding_credit_outbox_loop()))
        if settings.memory_enabled and settings.memory_ttl_days > 0:
            bg_tasks.append(asyncio.create_task(_memory_cleanup_loop()))
        if settings.marketplace_auto_sweep_enabled:
            from marketplace import _marketplace_sweep_loop

            bg_tasks.append(asyncio.create_task(_marketplace_sweep_loop()))
        if settings.reputation_rollup_enabled:
            bg_tasks.append(asyncio.create_task(_reputation_rollup_loop()))
        if settings.scheduled_runs_enabled:
            bg_tasks.append(
                asyncio.create_task(
                    _run_periodic(
                        "scheduled runs",
                        scheduled_runs_tick,
                        settings.scheduled_runs_tick_interval_seconds,
                        monitor_slug="scheduled-runs",
                    )
                )
            )
        if settings.agent_cache_prewarm_enabled:
            bg_tasks.append(asyncio.create_task(_prewarm_cache_prefixes(pool)))
        bg_tasks.append(asyncio.create_task(_refresh_token_cleanup_loop()))

        yield

        for task in bg_tasks:
            task.cancel()
        for task in bg_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        await close_agent_wallets_db()
        await close_benchmarks_db()
        await close_llm_config_db()
        await close_marketplace_db()
        await close_scheduling_db()
        await close_tool_exclusions_db()
        await close_memory_db()
        await close_mcp_client_db()
        await close_org_tools_db()
        await close_billing()
        await close_wallets_db()
        await close_usage_db()
        await close_user_db()
        await close_checkpointer()
        await close_redis()
        await pool.close()
        app.state.pool = None

        try:
            from tools._internals._http_session import close_http_sessions  # noqa: PLC0415

            await close_http_sessions()
        except Exception:
            logger.warning("Failed to close shared aiohttp sessions", exc_info=True)

        try:
            from tools._internals._web3_helpers import close_web3_clients  # noqa: PLC0415

            await close_web3_clients()
        except Exception:
            logger.warning("Failed to close web3 client sessions", exc_info=True)

    return lifespan
