# Database Migrations Catalog

Teardrop automatically manages database schemas utilizing SQL-based migration scripts. All schema changes reside in the [migrations/versions/](migrations/versions/) mapping.

To run migrations locally:
```powershell
python -m migrations.runner
```

## Migration Catalog

| File | Contents |
|------|----------|
| `001_baseline.sql` | Core tables: `orgs`, `users`, `wallets`, `siwe_nonces`, `usage_events` |
| `002_billing.sql` | Adds billing fields to `usage_events`; creates `pricing_rules` |
| `003_pricing_seed.sql` | Seeds default usage-based pricing (tokens_in, tokens_out, tool_call rates) |
| `004_credits.sql` | Adds `org_credits` table for prepaid credit balances |
| `005_org_client_credentials.sql` | Per-org M2M client credentials (`org_client_credentials`) |
| `006_credit_ledger.sql` | Immutable debit/top-up audit trail (`org_credit_ledger`) |
| `007_stripe_webhook_events.sql` | Stripe webhook idempotency table (`stripe_webhook_events`) |
| `008_usdc_topup_events.sql` | USDC on-chain top-up events (`usdc_topup_events`) |
| `009_a2a_delegation.sql` | A2A delegation allowlist support for remote agents (`a2a_allowed_agents`) |
| `009_tool_pricing_overrides.sql` | Per-tool pricing overrides; seeds web_search, get_token_price, get_wallet_portfolio rates |
| `010_org_tools.sql` | Per-org custom webhook tools (`org_tools`) and audit events |
| `011_org_memories.sql` | Enables `pgvector`; creates `org_memories` table with HNSW index |
| `012_org_mcp_servers.sql` | Per-org MCP server connections (`org_mcp_servers`) and audit events |
| `013_mcp_marketplace.sql` | Marketplace visibility flags (`publish_as_mcp`, `marketplace_description`, `base_price_usdc`) on org tools |
| `013_settlement_retry.sql` | Settlement retry tracking columns for auto-sweep background worker |
| `014_org_spending_limits.sql` | Per-org spending caps and pause/resume controls (`org_spending_limits`) |
| `015_memory_ttl_dedup.sql` | Memory TTL expiry and near-duplicate deduplication support |
| `016_email_verification.sql` | Email verification tokens and `email_verified` flag on users |
| `017_org_invites.sql` | Org invite tokens and acceptance flow (`org_invites`) |
| `018_refresh_tokens.sql` | Persistent refresh tokens for 30-day sessions (`refresh_tokens`) |
| `019_org_llm_config.sql` | Per-org LLM provider/model/BYOK config (`org_llm_config`) |
| `020_usage_provider_model.sql` | Adds `provider` and `model` columns to `usage_events` for per-model billing |
| `021_model_pricing.sql` | Dynamic per-model pricing table (`model_pricing`) |
| `022_model_pricing_seed.sql` | Seeds default pricing for all models in the catalogue |
| `023_siwe_login_sessions.sql` | SIWE session persistence for nonce replay protection |
| `024_a2a_delegation_billing.sql` | A2A delegation billing: extends `a2a_allowed_agents` with cost caps; creates `a2a_delegation_events` audit table |
| `025_org_agent_wallets.sql` | CDP-backed agent wallets (`org_agent_wallets`) and audit events (`agent_wallet_events`) |
| `026_a2a_jwt_forward.sql` | JWT forwarding flag on A2A delegation rules |
| `026_normalize_revenue_share.sql` | Backfills and normalises revenue share in basis points |
| `027_marketplace_tool_pricing.sql` | Per-tool pricing overrides for marketplace authors |
| `028_marketplace_subscriptions.sql` | Org marketplace tool subscriptions (`marketplace_subscriptions`) |
| `029_marketplace_platform_tools.sql` | Platform built-in metered tool enablement in marketplace |
| `029_sweep_retry_columns.sql` | Auto-sweep retry tracking and backoff columns on withdrawal records |
| `030_siwe_nonce_address_binding.sql` | Binds SIWE nonces to the signing address to prevent cross-wallet replay |
| `031_activate_bench_tools.sql` | Activates benchmark tooling entries |
| `031_byok_platform_fee.sql` | BYOK flat platform fee column on `org_llm_config` |
| `032_refresh_token_successor.sql` | Refresh token successor chaining for atomic rotation |
| `033_get_token_approvals.sql` | Schema support for `get_token_approvals` tool |
| `034_get_defi_positions.sql` | Schema support for `get_defi_positions` tool |
| `035_get_liquidation_risk.sql` | Schema support for `get_liquidation_risk` tool |
| `036_get_dex_quote.sql` | Schema support for `get_dex_quote` tool |
| `037_fix_haiku_pricing.sql` | Corrects Claude Haiku 4.5 token pricing to $0.80/$4.00 per 1k |
| `038_org_llm_config_allow_openrouter.sql` | Expands provider CHECK constraint to allow `openrouter` in `org_llm_config` |
| `039_new_model_pricing_seed.sql` | Pricing for DeepSeek V3.2 (superseded), Gemini 3 Flash Preview, and Claude Sonnet 4.6 |
| `040_marketplace_catalog_indexes.sql` | Compiles composite and pricing indexing for marketplace search/filters |
| `040_v4_flash_pricing.sql` | Replaces DeepSeek V3.2 pricing with V4 Flash (same Teardrop rates, lower provider cost) |
| `041_byok_tier_pricing.sql` | BYOK tier pricing: adds `is_byok BOOLEAN` to `pricing_rules`; seeds 5 provider-level BYOK rows at 50 atomic USDC/1k tokens |
| `042_org_tool_schema_hash.sql` | Org tool schema_hash + last_schema_changed_at tracking for change detection |
| `043_marketplace_subscription_schema_hash.sql` | Adds `subscribed_schema_hash TEXT` to `org_marketplace_subscriptions` |
| `044_gemini_3_flash_pricing_fix.sql` | Corrects `google-gemini-3-flash-preview-v1` from 125in/500out → 625in/3750out |
| `045_get_token_price_historical_seed.sql` | Seeds `get_token_price_historical` into marketplace_platform_tools at 4,000 atomic USDC ($0.004) |
| `046_web3_marketplace_seed.sql` | Marketplace seeding for web3 tools: `get_eth_balance`, `get_erc20_balance`, `get_block`, `get_transaction` |
| `047_get_protocol_tvl_seed.sql` | Seeds `get_protocol_tvl` into marketplace_platform_tools at 3,000 atomic USDC ($0.003) |
| `048_get_yield_rates_seed.sql` | Seeds `get_yield_rates` into marketplace_platform_tools at 4,000 atomic USDC ($0.004) |
| `049_org_tool_output_schema_validation.sql` | Org tool output schema validation support |
| `050_billable_tool_calls_accounting.sql` | Adds `billable_tool_calls`, `billable_tool_names`, `failed_tool_calls`, `failed_tool_names` to `usage_events` |
| `051_gpt54_mini_pricing_seed.sql` | Seeds `openai-gpt54-mini-v1`: 938in/5625out per 1k tokens, run_price=10000 atomic USDC |
| `052_get_lending_rates_marketplace_seed.sql` | `get_lending_rates` added to marketplace platform tools ($0.003) |
| `053_zero_cost_tool_overrides.sql` | Zero-cost `tool_pricing_overrides` for `calculate`, `get_datetime`, `count_text_stats` |
| `054_usage_events_cache_tokens.sql` | Adds `cache_read_tokens`, `cache_creation_tokens` to `usage_events` for telemetry |
| `055_org_tool_get_only_constraint.sql` | Enforces GET-only active org webhook tools (`chk_active_tool_get_only` constraint) |
| `056_web_search_price_alignment.sql` | Aligns `web_search` marketplace price to 15,000 atomic USDC ($0.015) |
| `057_credit_ledger_debit_index.sql` | Partial index `idx_credit_ledger_debit_time` on `org_credit_ledger(org_id, created_at DESC) WHERE operation='debit'` |
| `058_marketplace_dashboard_catalog.sql` | Public marketplace dashboard metadata: tool categories, aggregate call stats, catalog indexes, and platform category seeds |
| `059_x402_payment_nonces.sql` | x402 payment-header replay guard table (`x402_payment_nonces`) |
| `060_org_tools_partial_unique_name.sql` | Swaps table-level UNIQUE tool name constraint for partial index on active tools only |
| `061_marketplace_catalog_search.sql` | Trigram indexes for marketplace catalog free-text search (trgm across tool/author metadata) |
| `062_a2a_inbound_events.sql` | Inbound A2A audit ledger table (`a2a_inbound_events`) for caller identity and billing outcomes |
| `063_org_tools_mcp_backed.sql` | Extends `org_tools` to support MCP-backed tool references alongside webhooks |
| `064_scheduled_runs.sql` | Schedules and execution logs for recurring prompt runs (`scheduled_runs` & `scheduled_run_results`) |
| `065_event_triggers.sql` | Event-triggered reactive prompt runs via webhook dispatches (adds `trigger_token` & `secret_hash`) |
| `066_tool_call_events.sql` | Telemetry event logs (`tool_call_events`) for ML parameters and reputation tracking/user ratings (`run_feedback`) |
| `067_run_decisions.sql` | Per-run decision ledger with task, tool, and outcome labels for routing telemetry |
| `068_org_tool_exclusions.sql` | Persisted org-scoped tool exclusions |
| `069_onboarding_credit_grants.sql` | Idempotent verified-email onboarding-credit grants and retry outbox |
| `070_a2a_delegation_task_type.sql` | Bounded task type on immutable A2A delegation audit events |
| `071_mcp_client_schema_hash.sql` | External MCP inventory hash tracking for schema-drift detection |
| `072_reserve_platform_org_slug.sql` | Reserves the `platform` organization slug for built-in marketplace tools |
| `073_data_foundations.sql` | Run-source attribution, versioned ML telemetry, first-touch acquisition attribution, and retention support for disposable operational records |
| `074_usage_events_runner_version.sql` | Deployment version provenance for canonical agent-run records |
| `075_marketplace_reputation_v2.sql` | Recency-aware marketplace reputation diagnostics |
| `076_telemetry_run_starts.sql` | Source-split run-start denominator for telemetry completeness reporting |
| `077_telemetry_run_starts_retention.sql` | Ordered index for bounded telemetry run-start retention |
*** Add File: c:\Users\19788\Documents\Local Repositiories\teardrop\migrations\versions\077_telemetry_run_starts_retention.sql
-- Migration 077: retention index for telemetry completeness denominators
-- telemetry_run_starts is non-financial and retained only for the configured
-- completeness-reporting window. This index supports ordered, batched cleanup.

CREATE INDEX IF NOT EXISTS idx_telemetry_run_starts_started_at
	ON telemetry_run_starts (started_at);
