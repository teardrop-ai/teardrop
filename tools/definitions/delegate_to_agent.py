# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""delegate_to_agent – send a task to a remote A2A-compliant agent."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)


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


class DelegateToAgentOutput(BaseModel):
    agent_name: str = Field(description="Name of the remote agent (from its agent card)")
    status: str = Field(description="A2A task state: completed, failed, etc.")
    result: str = Field(description="Text result extracted from the remote agent's response")
    error: str | None = Field(default=None, description="Error message, if any")
    cost_usdc: int = Field(default=0, description="Cost of this delegation in atomic USDC")


# ─── Implementation ──────────────────────────────────────────────────────────


async def delegate_to_agent(agent_url: str, task_description: str, *, config: dict | None = None) -> dict[str, Any]:
    """Delegate a task to a remote A2A agent and return the result.

    This tool discovers the remote agent's capabilities via its published agent
    card, sends a message using the A2A HTTP+JSON/REST binding, and returns the
    agent's response.  When delegation billing is enabled, enforces the org's
    allowlist, checks budget, debits credits, and records an audit event.
    """
    from config import get_settings

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
    from a2a_client import async_validate_url

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

    # ── Allowlist + billing pre-flight ────────────────────────────────────
    billing_enabled = settings.a2a_delegation_billing_enabled and org_id and db_pool
    agent_rule: dict | None = None
    allowed = False
    use_x402 = False
    estimated_cost = settings.a2a_delegation_max_cost_usdc

    # ── Allowlist check (independent of billing) ─────────────────────────
    if org_id and db_pool:
        from a2a_client import check_delegation_allowed

        allowed, agent_rule = await check_delegation_allowed(org_id, agent_url, db_pool)
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
    from a2a_client import discover_agent_card, extract_result_text, send_message

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
    cost_usdc = 0
    try:
        if use_x402:
            from a2a_client import send_message_with_payment
            from billing import get_treasury_signer

            signer = get_treasury_signer()
            response = await send_message_with_payment(
                agent_url,
                task_description,
                signer=signer,
                timeout=settings.a2a_delegation_timeout_seconds,
                auth_header=auth_header_to_forward,
            )
        else:
            response = await send_message(
                agent_url,
                task_description,
                timeout=settings.a2a_delegation_timeout_seconds,
                auth_header=auth_header_to_forward,
            )
    except Exception as exc:
        logger.warning("delegate_to_agent: message send failed for %s: %s", agent_url, exc)
        # Record failed event if billing is enabled.
        if billing_enabled:
            from billing import record_delegation_event

            await record_delegation_event(
                org_id=org_id,
                run_id=run_id,
                agent_url=agent_url,
                agent_name=card.name,
                task_status="failed",
                cost_usdc=0,
                error=str(exc),
            )
        return {
            "agent_name": card.name,
            "status": "failed",
            "result": "",
            "error": f"Failed to send message to {card.name}: {exc}",
            "cost_usdc": 0,
        }

    # ── Extract result ────────────────────────────────────────────────────
    task_state = "completed"
    if response.task:
        task_state = response.task.status.state

    result_text = extract_result_text(response)

    # ── Post-delegation billing: debit credits + audit ────────────────────
    if billing_enabled and task_state == "completed":
        from billing import apply_platform_fee, fund_delegation, record_delegation_event

        # Use per-agent cap (or global default) as the cost for now.
        raw_cost = (
            agent_rule["max_cost_usdc"]
            if agent_rule and agent_rule.get("max_cost_usdc", 0) > 0
            else settings.a2a_delegation_max_cost_usdc
        )
        cost_usdc = apply_platform_fee(raw_cost)
        funded = await fund_delegation(org_id, cost_usdc, run_id, agent_url)
        if not funded:
            cost_usdc = 0
            logger.warning(
                "delegate_to_agent: fund_delegation failed org=%s cost=%s",
                org_id,
                cost_usdc,
            )

        await record_delegation_event(
            org_id=org_id,
            run_id=run_id,
            agent_url=agent_url,
            agent_name=card.name,
            task_status=task_state,
            cost_usdc=cost_usdc,
            billing_method="x402" if use_x402 else "credit",
        )
    elif billing_enabled:
        from billing import record_delegation_event

        await record_delegation_event(
            org_id=org_id,
            run_id=run_id,
            agent_url=agent_url,
            agent_name=card.name,
            task_status=task_state,
            cost_usdc=0,
            error=f"Remote agent state: {task_state}",
        )

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
    implementation=delegate_to_agent,
)
