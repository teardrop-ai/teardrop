# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""delegate_to_agent – send a task to a remote A2A-compliant agent."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_delegation_semaphores: weakref.WeakValueDictionary[tuple[int, str], asyncio.Semaphore] = weakref.WeakValueDictionary()
_delegation_semaphore_lock = threading.Lock()

DelegationTaskType = Literal[
    "general",
    "research",
    "analysis",
    "data_retrieval",
    "coding",
    "transaction",
    "automation",
]


# ─── Schemas ──────────────────────────────────────────────────────────────────


class DelegateToAgentInput(BaseModel):
    agent_url: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Base URL of the remote A2A agent (e.g. https://agent.example.com)",
    )
    task_description: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Natural language description of the task to delegate",
    )
    task_type: DelegationTaskType = Field(
        default="general",
        description="Broad task class for routing telemetry; never include user data or task text.",
    )


class DelegateToAgentOutput(BaseModel):
    agent_name: str = Field(description="Name of the remote agent (from its agent card)")
    status: str = Field(description="A2A task state: completed, failed, etc.")
    result: str = Field(description="Text result extracted from the remote agent's response")
    error: str | None = Field(default=None, description="Error message, if any")
    cost_usdc: int = Field(default=0, description="Cost of this delegation in atomic USDC")


# ─── Implementation ──────────────────────────────────────────────────────────


def _get_delegation_semaphore(run_id: str, limit: int) -> asyncio.Semaphore | None:
    """Return the shared delegation limiter for one run and event loop."""
    if not run_id:
        return None
    key = (id(asyncio.get_running_loop()), run_id)
    with _delegation_semaphore_lock:
        semaphore = _delegation_semaphores.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            _delegation_semaphores[key] = semaphore
    return semaphore


async def _send_with_limits(
    send_call: Callable[[], Awaitable[Any]],
    *,
    run_id: str,
    timeout_seconds: float,
    max_concurrent: int,
) -> Any:
    """Limit same-run fan-out and bound the full remote exchange."""
    semaphore = _get_delegation_semaphore(run_id, max_concurrent)
    if semaphore is None:
        return await asyncio.wait_for(send_call(), timeout=timeout_seconds)
    async with semaphore:
        return await asyncio.wait_for(send_call(), timeout=timeout_seconds)


async def delegate_to_agent(
    agent_url: str,
    task_description: str,
    task_type: DelegationTaskType = "general",
    *,
    config: dict | None = None,
) -> dict[str, Any]:
    """Delegate a task to a remote A2A agent and return the result.

    This tool discovers the remote agent's capabilities via its published agent
    card, sends a message using the A2A HTTP+JSON/REST binding, and returns the
    agent's response.  When delegation billing is enabled, enforces the org's
    allowlist, checks budget, debits credits, and records an audit event.
    """
    from teardrop.config import get_settings

    settings = get_settings()

    # ── Feature flag ──────────────────────────────────────────────────────
    if not settings.a2a_delegation_enabled:
        return {
            "agent_name": "unknown",
            "status": "failed",
            "result": "",
            "error": "A2A delegation is not enabled. Set A2A_DELEGATION_ENABLED=true.",
            "cost_usdc": 0,
        }

    # ── SSRF check ────────────────────────────────────────────────────────
    from teardrop.a2a_client import async_validate_url

    ssrf_err = await async_validate_url(agent_url)
    if ssrf_err:
        return {
            "agent_name": "unknown",
            "status": "failed",
            "result": "",
            "error": f"Blocked URL: {ssrf_err}",
            "cost_usdc": 0,
        }

    # ── Extract org context from RunnableConfig ───────────────────────────
    org_id: str = ""
    run_id: str = ""
    db_pool = None
    jwt_token: str | None = None
    if config:
        configurable = config.get("configurable", {})
        org_id = configurable.get("org_id", "")
        run_id = configurable.get("run_id", "")
        db_pool = configurable.get("db_pool")
        jwt_token = configurable.get("jwt_token")

    if settings.a2a_delegation_require_allowlist and not (org_id and db_pool):
        return {
            "agent_name": "unknown",
            "status": "failed",
            "result": "",
            "error": "Delegation requires authenticated organisation context and an allowlist entry.",
            "cost_usdc": 0,
        }

    # ── Allowlist + billing pre-flight ────────────────────────────────────
    billing_enabled = settings.a2a_delegation_billing_enabled and org_id and db_pool
    agent_rule: dict | None = None
    allowed = False
    use_x402 = False
    estimated_cost = settings.a2a_delegation_max_cost_usdc

    # ── Allowlist check (independent of billing) ─────────────────────────
    if org_id and db_pool:
        from teardrop.a2a_client import check_delegation_allowed

        try:
            allowed, agent_rule = await check_delegation_allowed(org_id, agent_url, db_pool)
        except Exception:
            logger.exception("delegate_to_agent: allowlist lookup failed org=%s", org_id)
            return {
                "agent_name": "unknown",
                "status": "failed",
                "result": "",
                "error": "Unable to verify the delegation allowlist. Try again later.",
                "cost_usdc": 0,
            }
        if not allowed and settings.a2a_delegation_require_allowlist:
            return {
                "agent_name": "unknown",
                "status": "failed",
                "result": "",
                "error": (
                    f"Agent {agent_url} is not in your organisation's allowed agents list. Add it via POST /a2a/agents first."
                ),
                "cost_usdc": 0,
            }

    # ── JWT forwarding: resolve from allowlist rule ───────────────────────
    auth_header_to_forward: str | None = None
    if agent_rule and agent_rule.get("jwt_forward") and jwt_token:
        auth_header_to_forward = jwt_token
    elif agent_rule and agent_rule.get("jwt_forward") and not jwt_token:
        logger.warning(
            "delegate_to_agent: jwt_forward=true for %s but no JWT available",
            agent_url,
        )

    if billing_enabled:
        from billing import apply_platform_fee, check_delegation_budget

        if not allowed:
            return {
                "agent_name": "unknown",
                "status": "failed",
                "result": "",
                "error": (
                    f"Agent {agent_url} is not in your organisation's allowed agents list. Add it via POST /a2a/agents first."
                ),
                "cost_usdc": 0,
            }

        # Per-agent cost cap overrides global default.
        if agent_rule and agent_rule.get("max_cost_usdc", 0) > 0:
            estimated_cost = agent_rule["max_cost_usdc"]

        estimated_cost = apply_platform_fee(estimated_cost)
        use_x402 = bool(agent_rule and agent_rule.get("require_x402"))

        budget_err = await check_delegation_budget(org_id, estimated_cost)
        if budget_err:
            return {
                "agent_name": "unknown",
                "status": "failed",
                "result": "",
                "error": budget_err,
                "cost_usdc": 0,
            }

    # ── Discover agent card ───────────────────────────────────────────────
    from teardrop.a2a_client import discover_agent_card, extract_result_text, send_message

    try:
        card = await discover_agent_card(
            agent_url,
            timeout=min(10, settings.a2a_delegation_timeout_seconds),
            cache_ttl=settings.a2a_agent_card_cache_ttl_seconds,
        )
    except Exception as exc:
        logger.warning("delegate_to_agent: agent card discovery failed for %s: %s", agent_url, exc)
        return {
            "agent_name": "unknown",
            "status": "failed",
            "result": "",
            "error": f"Could not discover agent card at {agent_url}: {exc}",
            "cost_usdc": 0,
        }

    # ── Send message (with x402 payment if required) ──────────────────────
    # Pre-debit: charge the org BEFORE dispatching to the remote agent so a
    # post-execution debit failure (e.g. a concurrent debit draining the
    # balance below the non-locking budget snapshot) cannot yield a free
    # delegation. The charge is refunded below if dispatch fails or the remote
    # does not complete.
    cost_usdc = 0
    delegation_id = str(uuid.uuid4()) if billing_enabled else ""
    if billing_enabled:
        from billing import fund_delegation

        funded = await fund_delegation(org_id, estimated_cost, run_id, agent_url, delegation_id)
        if not funded:
            from billing import record_delegation_event

            logger.warning("delegate_to_agent: pre-debit failed org=%s cost=%s", org_id, estimated_cost)
            await record_delegation_event(
                org_id=org_id,
                run_id=run_id,
                agent_url=agent_url,
                agent_name=card.name,
                task_status="failed",
                cost_usdc=0,
                error="Insufficient credit for delegation at debit time.",
                task_type=task_type,
                delegation_id=delegation_id,
            )
            return {
                "agent_name": card.name,
                "status": "failed",
                "result": "",
                "error": "Insufficient credit for delegation. Top up via POST /admin/credits/topup.",
                "cost_usdc": 0,
            }
        cost_usdc = estimated_cost

    payment_attempted = False

    async def _on_payment_attempt() -> None:
        nonlocal payment_attempted
        from billing import mark_delegation_possibly_delivered

        if not await mark_delegation_possibly_delivered(org_id, delegation_id):
            raise RuntimeError("Could not persist the x402 delivery state before dispatch.")
        payment_attempted = True

    async def _send_remote_message() -> Any:
        if use_x402:
            from billing import get_treasury_signer
            from teardrop.a2a_client import send_message_with_payment

            signer = get_treasury_signer()
            return await send_message_with_payment(
                agent_url,
                task_description,
                signer=signer,
                timeout=settings.a2a_delegation_timeout_seconds,
                auth_header=auth_header_to_forward,
                max_amount_atomic=estimated_cost,
                allowed_networks=frozenset({settings.x402_network}),
                payment_attempt_callback=_on_payment_attempt,
            )
        return await send_message(
            agent_url,
            task_description,
            timeout=settings.a2a_delegation_timeout_seconds,
            auth_header=auth_header_to_forward,
        )

    try:
        response = await _send_with_limits(
            _send_remote_message,
            run_id=run_id,
            timeout_seconds=float(settings.a2a_delegation_timeout_seconds),
            max_concurrent=settings.a2a_delegation_max_concurrent_per_run,
        )
    except Exception as exc:
        reason = f"timed out after {settings.a2a_delegation_timeout_seconds}s" if isinstance(exc, TimeoutError) else str(exc)
        logger.warning("delegate_to_agent: message send failed for %s: %s", agent_url, reason)
        ambiguous_delivery = bool(billing_enabled and use_x402 and payment_attempted)
        if billing_enabled:
            from billing import record_delegation_event, refund_delegation

            if ambiguous_delivery:
                await record_delegation_event(
                    org_id=org_id,
                    run_id=run_id,
                    agent_url=agent_url,
                    agent_name=card.name,
                    task_status="possibly_delivered",
                    cost_usdc=cost_usdc,
                    billing_method="x402",
                    error=reason,
                    task_type=task_type,
                    delegation_id=delegation_id,
                )
                return {
                    "agent_name": card.name,
                    "status": "possibly_delivered",
                    "result": "",
                    "error": "x402 delivery outcome is ambiguous; operator reconciliation is required before refunding.",
                    "cost_usdc": cost_usdc,
                }

            await record_delegation_event(
                org_id=org_id,
                run_id=run_id,
                agent_url=agent_url,
                agent_name=card.name,
                task_status="failed",
                cost_usdc=0,
                error=reason,
                task_type=task_type,
                delegation_id=delegation_id,
            )
            refund_completed = await refund_delegation(org_id, cost_usdc, run_id, delegation_id)
            cost_usdc = 0
            if not refund_completed:
                logger.error("delegate_to_agent: refund queued for retry org=%s delegation=%s", org_id, delegation_id)
        return {
            "agent_name": card.name,
            "status": "failed",
            "result": "",
            "error": f"Failed to send message to {card.name}: {reason}",
            "cost_usdc": 0,
        }

    # ── Extract result ────────────────────────────────────────────────────
    task_state = "completed"
    if response.task:
        task_state = response.task.status.state

    result_text = extract_result_text(response)
    settlement_tx = getattr(response, "settlement_tx", "")

    # ── Post-delegation audit (charge already taken pre-dispatch) ──────────
    if billing_enabled and task_state == "completed":
        from billing import cancel_delegation_refund, record_delegation_event, refund_delegation

        audit_recorded = await record_delegation_event(
            org_id=org_id,
            run_id=run_id,
            agent_url=agent_url,
            agent_name=card.name,
            task_status=task_state,
            cost_usdc=cost_usdc,
            billing_method="x402" if use_x402 else "credit",
            settlement_tx=settlement_tx,
            task_type=task_type,
            delegation_id=delegation_id,
        )
        if not audit_recorded:
            if use_x402 and payment_attempted:
                from billing import confirm_delegation_delivery

                if not await confirm_delegation_delivery(org_id, delegation_id, settlement_tx):
                    logger.error(
                        "delegate_to_agent: delivery confirmation queued for retry org=%s delegation=%s",
                        org_id,
                        delegation_id,
                    )
                return {
                    "agent_name": card.name,
                    "status": task_state,
                    "result": result_text,
                    "error": "Delegation audit could not be recorded; delivery retained for reconciliation.",
                    "cost_usdc": cost_usdc,
                }
            refund_completed = await refund_delegation(org_id, cost_usdc, run_id, delegation_id)
            cost_usdc = 0
            if not refund_completed:
                logger.error("delegate_to_agent: refund queued for retry org=%s delegation=%s", org_id, delegation_id)
            return {
                "agent_name": card.name,
                "status": "failed",
                "result": "",
                "error": "Delegation audit could not be recorded; pre-debit refunded.",
                "cost_usdc": 0,
            }
        if use_x402:
            from billing import confirm_delegation_delivery

            if not await confirm_delegation_delivery(org_id, delegation_id, settlement_tx):
                logger.error(
                    "delegate_to_agent: delivery confirmation queued for retry org=%s delegation=%s",
                    org_id,
                    delegation_id,
                )
        elif not await cancel_delegation_refund(org_id, delegation_id):
            logger.error("delegate_to_agent: completion cancel queued for retry org=%s delegation=%s", org_id, delegation_id)
    elif billing_enabled:
        # Remote agent did not complete — refund the pre-debit.
        from billing import record_delegation_event, refund_delegation

        await record_delegation_event(
            org_id=org_id,
            run_id=run_id,
            agent_url=agent_url,
            agent_name=card.name,
            task_status=task_state,
            cost_usdc=0,
            billing_method="x402" if use_x402 else "credit",
            settlement_tx=settlement_tx,
            error=f"Remote agent state: {task_state}",
            task_type=task_type,
            delegation_id=delegation_id,
        )
        if use_x402 and payment_attempted:
            from billing import fail_delegation_delivery

            refund_completed = await fail_delegation_delivery(
                org_id,
                delegation_id,
                f"Remote agent state: {task_state}",
            )
        else:
            refund_completed = await refund_delegation(org_id, cost_usdc, run_id, delegation_id)
        cost_usdc = 0
        if not refund_completed:
            logger.error("delegate_to_agent: refund queued for retry org=%s delegation=%s", org_id, delegation_id)

    return {
        "agent_name": card.name,
        "status": task_state,
        "result": result_text,
        "error": None if task_state in ("completed",) else f"Remote agent state: {task_state}",
        "cost_usdc": cost_usdc,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="delegate_to_agent",
    version="1.0.0",
    description=(
        "Delegate a task to a remote A2A-compliant agent. Discovers the agent's "
        "capabilities via its agent card, sends it a message, and returns the result. "
        "Use when a task requires specialist capabilities beyond your own tools."
    ),
    tags=["a2a", "delegation", "agent"],
    input_schema=DelegateToAgentInput,
    output_schema=DelegateToAgentOutput,
    annotations={
        "readOnlyHint": False,
        "openWorldHint": True,
        "idempotentHint": False,
    },
    implementation=delegate_to_agent,
)
