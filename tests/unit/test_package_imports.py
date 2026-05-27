# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Smoke-test package import compatibility after root-to-package refactor."""

from __future__ import annotations

import importlib

import pytest

BILLING_IMPORT_SURFACE = [
    "BillingResult",
    "admin_topup_credit",
    "build_402_headers",
    "build_402_response_body",
    "build_usdc_topup_requirements",
    "calculate_byok_orchestration_cost",
    "calculate_run_cost_usdc",
    "close_billing",
    "create_stripe_embedded_session",
    "credit_usdc_topup",
    "debit_credit",
    "delete_tool_pricing_override",
    "enqueue_failed_settlement",
    "get_billing_history",
    "get_byok_platform_fee",
    "get_credit_balance",
    "get_credit_history",
    "get_current_pricing",
    "get_delegation_events",
    "get_invoice_by_run",
    "get_invoices",
    "get_org_spending_config",
    "get_pending_settlements",
    "get_revenue_summary",
    "get_stripe_session_status",
    "get_tool_pricing_overrides",
    "handle_stripe_webhook",
    "init_billing",
    "process_pending_settlements",
    "record_settlement",
    "reset_exhausted_settlement",
    "resolve_tool_cost",
    "settle_payment",
    "update_org_spending_config",
    "upsert_tool_pricing_override",
    "verify_and_settle_usdc_topup",
    "verify_credit",
    "verify_payment",
    "verify_settlement_on_chain",
]

MARKETPLACE_IMPORT_SURFACE = [
    "AuthorEarningByTool",
    "AuthorWithdrawal",
    "_build_catalog_cursor",
    "_marketplace_sweep_loop",
    "build_subscribed_marketplace_tools",
    "check_org_subscription",
    "close_marketplace_db",
    "complete_withdrawal",
    "get_author_balance",
    "get_author_config",
    "get_author_earnings_by_tool",
    "get_author_earnings_history",
    "get_marketplace_author_summary",
    "get_marketplace_catalog",
    "get_marketplace_catalog_tool",
    "get_marketplace_tool_by_name",
    "get_org_subscriptions",
    "get_subscribed_tools_catalog",
    "init_marketplace_db",
    "list_exhausted_withdrawals",
    "list_org_withdrawals",
    "list_pending_withdrawals",
    "marketplace_sweep_once",
    "process_withdrawal",
    "record_marketplace_tool_call",
    "record_marketplace_tool_usage",
    "record_marketplace_tool_usage_many",
    "record_tool_call_earnings",
    "request_withdrawal",
    "reset_withdrawal",
    "set_author_config",
    "subscribe_to_tool",
    "unsubscribe_from_tool",
]

MCP_CLIENT_IMPORT_SURFACE = [
    "OrgMcpServer",
    "build_mcp_langchain_tools",
    "close_mcp_client_db",
    "create_org_mcp_server",
    "delete_org_mcp_server",
    "discover_mcp_tools",
    "get_org_mcp_server",
    "init_mcp_client_db",
    "list_org_mcp_servers",
    "update_org_mcp_server",
]

ORG_TOOLS_IMPORT_SURFACE = [
    "OrgTool",
    "_decrypt_header",
    "_hash_webhook_host",
    "_on_webhook_failure",
    "_record_event",
    "build_org_langchain_tools",
    "close_org_tools_db",
    "create_org_tool",
    "delete_org_tool",
    "get_org_tool",
    "init_org_tools_db",
    "invalidate_org_tools_cache",
    "list_marketplace_tools",
    "list_org_tools",
    "normalize_webhook_response",
    "update_org_tool",
    "validate_safe_schema_subset",
]

SHARED_SUBMODULE_SURFACE: dict[str, list[str]] = {
    "shared.db_pool": ["bind_pool", "get_bound_pool", "require_pool", "unbind_pool"],
    "shared.audit": ["insert_event_row"],
    "shared.webhook": ["WebhookCaller", "WebhookCallError", "WebhookCallResult"],
}


@pytest.mark.parametrize("name", BILLING_IMPORT_SURFACE)
def test_billing_import_surface(name: str) -> None:
    module = importlib.import_module("billing")
    assert hasattr(module, name), f"billing.{name} missing"


@pytest.mark.parametrize("name", MARKETPLACE_IMPORT_SURFACE)
def test_marketplace_import_surface(name: str) -> None:
    module = importlib.import_module("marketplace")
    assert hasattr(module, name), f"marketplace.{name} missing"


@pytest.mark.parametrize("name", MCP_CLIENT_IMPORT_SURFACE)
def test_mcp_client_import_surface(name: str) -> None:
    module = importlib.import_module("mcp_client")
    assert hasattr(module, name), f"mcp_client.{name} missing"


@pytest.mark.parametrize("name", ORG_TOOLS_IMPORT_SURFACE)
def test_org_tools_import_surface(name: str) -> None:
    module = importlib.import_module("org_tools")
    assert hasattr(module, name), f"org_tools.{name} missing"


@pytest.mark.parametrize("module_name,symbols", SHARED_SUBMODULE_SURFACE.items())
def test_shared_submodule_import_surface(module_name: str, symbols: list[str]) -> None:
    module = importlib.import_module(module_name)
    for symbol in symbols:
        assert hasattr(module, symbol), f"{module_name}.{symbol} missing"
