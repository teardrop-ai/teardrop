---
topic: security
status: draft
generated_at_utc: "2026-08-05T01:53:44.045236+00:00"
evidence_commit: "d99ea5fb29143e30fda50b4471a89089582f54f7"
evidence_dirty: true
source_manifest_sha256: "79ccaf4db208ab2ac12a6c581973d446c62bc58bd9aebf8e39e30596de3196d0"
source_redaction_count: 0
report_source: "local"
prompt_version: 1
query: "Can a caller double-debit or lose a settlement in Teardrop's credit and x402 billing paths?"
---

# Security architecture audit

> This report is unverified research. Do not treat a suspected security issue as confirmed until a human verifies it against the cited live code.

## Research scope

- `billing/__init__.py`
- `billing/context.py`
- `billing/credit.py`
- `billing/delegation.py`
- `billing/history.py`
- `billing/models.py`
- `billing/pricing.py`
- `billing/settlement.py`
- `billing/stripe.py`
- `billing/x402.py`
- `marketplace/__init__.py`
- `marketplace/_catalog_pricing.py`
- `marketplace/catalog.py`
- `marketplace/context.py`
- `marketplace/earnings.py`
- `marketplace/models.py`
- `marketplace/reputation.py`
- `marketplace/stats.py`
- `marketplace/subscriptions.py`
- `marketplace/withdrawals.py`
- `marketplace/worker.py`
- `mcp_client/__init__.py`
- `mcp_client/base.py`
- `mcp_client/cache.py`
- `mcp_client/crud.py`
- `mcp_client/runtime.py`
- `mcp_client/session.py`
- `org_tools/__init__.py`
- `org_tools/base.py`
- `org_tools/cache.py`
- `org_tools/crud.py`
- `org_tools/runtime.py`
- `teardrop/app.py`
- `teardrop/a2a_client.py`
- `teardrop/mcp_gateway.py`
- `migrations/versions/001_baseline.sql`
- `migrations/versions/002_billing.sql`
- `migrations/versions/003_pricing_seed.sql`
- `migrations/versions/004_credits.sql`
- `migrations/versions/005_org_client_credentials.sql`
- `migrations/versions/006_credit_ledger.sql`
- `migrations/versions/007_stripe_webhook_events.sql`
- `migrations/versions/008_usdc_topup_events.sql`
- `migrations/versions/009_a2a_delegation.sql`
- `migrations/versions/009_tool_pricing_overrides.sql`
- `migrations/versions/010_org_tools.sql`
- `migrations/versions/011_org_memories.sql`
- `migrations/versions/012_org_mcp_servers.sql`
- `migrations/versions/013_mcp_marketplace.sql`
- `migrations/versions/013_settlement_retry.sql`
- `migrations/versions/014_org_spending_limits.sql`
- `migrations/versions/015_memory_ttl_dedup.sql`
- `migrations/versions/016_email_verification.sql`
- `migrations/versions/017_org_invites.sql`
- `migrations/versions/018_refresh_tokens.sql`
- `migrations/versions/019_org_llm_config.sql`
- `migrations/versions/020_usage_provider_model.sql`
- `migrations/versions/021_model_pricing.sql`
- `migrations/versions/022_model_pricing_seed.sql`
- `migrations/versions/023_siwe_login_sessions.sql`
- `migrations/versions/024_a2a_delegation_billing.sql`
- `migrations/versions/025_org_agent_wallets.sql`
- `migrations/versions/026_a2a_jwt_forward.sql`
- `migrations/versions/026_normalize_revenue_share.sql`
- `migrations/versions/027_marketplace_tool_pricing.sql`
- `migrations/versions/028_marketplace_subscriptions.sql`
- `migrations/versions/029_marketplace_platform_tools.sql`
- `migrations/versions/029_sweep_retry_columns.sql`
- `migrations/versions/030_siwe_nonce_address_binding.sql`
- `migrations/versions/031_activate_bench_tools.sql`
- `migrations/versions/031_byok_platform_fee.sql`
- `migrations/versions/032_refresh_token_successor.sql`
- `migrations/versions/033_get_token_approvals.sql`
- `migrations/versions/034_get_defi_positions.sql`
- `migrations/versions/035_get_liquidation_risk.sql`
- `migrations/versions/036_get_dex_quote.sql`
- `migrations/versions/037_fix_haiku_pricing.sql`
- `migrations/versions/038_org_llm_config_allow_openrouter.sql`
- `migrations/versions/039_new_model_pricing_seed.sql`
- `migrations/versions/040_marketplace_catalog_indexes.sql`
- `migrations/versions/040_v4_flash_pricing.sql`
- `migrations/versions/041_byok_tier_pricing.sql`
- `migrations/versions/042_org_tools_schema_hash.sql`
- `migrations/versions/043_marketplace_subscription_schema_hash.sql`
- `migrations/versions/044_fix_gemini3_flash_pricing.sql`
- `migrations/versions/045_get_token_price_historical.sql`
- `migrations/versions/046_web3_tools_marketplace_seed.sql`
- `migrations/versions/047_get_protocol_tvl.sql`
- `migrations/versions/048_get_yield_rates.sql`
- `migrations/versions/049_org_tools_output_schema.sql`
- `migrations/versions/050_usage_events_billable_accounting.sql`
- `migrations/versions/051_gpt54_mini_pricing_seed.sql`
- `migrations/versions/052_get_lending_rates.sql`
- `migrations/versions/053_zero_cost_inprocess_tool_overrides.sql`
- `migrations/versions/054_usage_events_cache_tokens.sql`
- `migrations/versions/055_get_only_webhook_tools.sql`
- `migrations/versions/056_align_web_search_marketplace_price.sql`
- `migrations/versions/057_credit_ledger_debit_index.sql`
- `migrations/versions/058_marketplace_dashboard_catalog.sql`
- `migrations/versions/059_x402_payment_nonces.sql`
- `migrations/versions/060_org_tools_partial_unique_name.sql`
- `migrations/versions/061_marketplace_catalog_search.sql`
- `migrations/versions/062_a2a_inbound_events.sql`
- `migrations/versions/063_org_tools_mcp_backed.sql`
- `migrations/versions/064_scheduled_runs.sql`
- `migrations/versions/065_event_triggers.sql`
- `migrations/versions/066_tool_call_events.sql`
- `migrations/versions/067_run_decisions.sql`
- `migrations/versions/068_org_tool_exclusions.sql`
- `migrations/versions/069_onboarding_credit_grants.sql`
- `migrations/versions/070_a2a_delegation_task_type.sql`
- `migrations/versions/071_mcp_client_schema_hash.sql`
- `migrations/versions/072_reserve_platform_org_slug.sql`
- `migrations/versions/073_data_foundations.sql`
- `migrations/versions/074_usage_events_runner_version.sql`
- `migrations/versions/075_marketplace_reputation_v2.sql`
- `migrations/versions/076_telemetry_run_starts.sql`
- `migrations/versions/077_telemetry_run_starts_retention.sql`
- `migrations/versions/078_run_decisions_thread_message.sql`
- `migrations/versions/079_gemini_3_6_flash_pricing.sql`
- `migrations/versions/080_claude_sonnet_5_pricing.sql`
- `migrations/versions/081_reputation_public_metrics.sql`
- `migrations/versions/082_deepseek_v4_flash_0731_openrouter_us_pricing.sql`
- `.github/skills/teardrop-domain-invariants/SKILL.md`

## Report

## Executive conclusion

The credit debit path is well-guarded against concurrent double-spend: `debit_credit` uses `SELECT ... FOR UPDATE` row locking, a strict no-partial-debit check, and an in-transaction spending-limit check. Top-up replay is protected by `ON CONFLICT (tx_hash) DO NOTHING`. However, two settlement-integrity gaps are plausible and warrant verification: (1) the settlement retry worker explicitly refuses to retry x402 settlements, which could strand a caller who paid on-chain but whose credit/record write failed; and (2) credit debit retries lack an idempotency key on the ledger `reason` column, so a crash between debit commit and settlement-status update could double-debit. Both are classified `inconclusive` because the manifest does not show the full settlement transaction boundaries or unique constraints.

## Findings

### 1. x402 settlement retry refusal may strand paid settlements
- **Severity/Priority:** High
- **Confidence:** Medium
- **Classification:** `inconclusive`
- **Claim:** The retry worker marks x402 settlements as exhausted and never retries them, even when the initial failure was transient (e.g., network error after on-chain broadcast). A caller who paid on-chain but whose DB record/credit write failed could lose the settlement.
- **Evidence path/symbol:** `billing/settlement.py` — retry loop: `if billing_method == "credit": ... else: error_msg = "x402 settlements cannot be retried after initial failure"; retry_count = max_retries`
- **Impact:** Caller pays USDC on-chain but receives no credit or record; funds are lost from the caller's perspective.
- **Why evidence supports claim:** The manifest explicitly shows the worker refusing to retry x402 and forcing `retry_count = max_retries`, which exhausts the item. The manifest does not show an alternative recovery path (e.g., reconciliation job) for x402 failures, so the risk is plausible but unverified.

### 2. Credit debit retry lacks an idempotency key on the ledger
- **Severity/Priority:** High
- **Confidence:** Medium
- **Classification:** `inconclusive`
- **Claim:** The retry worker calls `debit_credit(org_id, amount, reason=f"run:{run_id}")`. If the worker crashes after the debit transaction commits but before marking the `pending_settlements` row as settled, the retry will debit again. The ledger insert uses `str(uuid.uuid4())` as the row id with no unique constraint on `reason`, so there is no idempotency guard.
- **Evidence path/symbol:** `billing/settlement.py` retry loop (credit branch) + `billing/credit.py` ledger insert (`INSERT INTO org_credit_ledger (id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at) VALUES ($1, ...)` with `str(uuid.uuid4())`).
- **Impact:** Double-debit of the org credit balance on retry after a crash between commit and status update.
- **Why evidence supports claim:** The manifest shows the retry re-invokes `debit_credit` with the same `run_id` reason but no unique constraint or `ON CONFLICT` on that reason. The exact transaction boundary between debit commit and settlement-status update is not shown, so the crash window is unverified.

### 3. On-chain top-up settlement loss on credit-write failure
- **Severity/Priority:** Medium
- **Confidence:** Low
- **Classification:** `inconclusive`
- **Claim:** `verify_and_settle_usdc_topup` settles on-chain and then `credit_usdc_topup` writes the credit with `ON CONFLICT (tx_hash) DO NOTHING`. If the credit write fails after on-chain settlement (e.g., DB outage), the caller paid but received no credit, and there is no visible retry path for the credit step.
- **Evidence path/symbol:** `billing/x402.py` — `verify_and_settle_usdc_topup` (settlement then credit) and `credit_usdc_topup` (`ON CONFLICT (tx_hash) DO NOTHING`).
- **Impact:** Caller loses the top-up payment if the post-settlement DB write fails and no reconciliation exists.
- **Why evidence supports claim:** The manifest shows settlement and credit as separate steps with replay protection only on the credit side; it does not show a retry or reconciliation mechanism for the credit step after a failed write.

### 4. Preflight credit check is non-locking but backstopped by strict debit
- **Severity/Priority:** Informational
- **Confidence:** High
- **Classification:** `verified` (control, not vulnerability)
- **Claim:** `verify_credit` performs a non-locking balance check before the run, but `debit_credit` re-checks under `SELECT FOR UPDATE` and rejects if `original_balance < amount_usdc`, preventing the concurrent-debit race from draining below zero.
- **Evidence path/symbol:** `billing/credit.py` — `verify_credit` (balance check) and `debit_credit` (`SELECT ... FOR UPDATE`, strict `if original_balance < amount_usdc: return False, 0`).
- **Impact:** No double-spend; the preflight may over-approve but the debit is authoritative.
- **Why evidence supports claim:** The manifest explicitly documents the strict-debit rationale: "This closes the concurrent-debit race where two runs both pass the non-locking preflight and the second drains to zero."

## Verified controls

- **Row-locking debit:** `billing/credit.py` `debit_credit` uses `SELECT balance_usdc, spending_limit_usdc, is_paused FROM org_credits WHERE org_id = $1 FOR UPDATE` inside a transaction.
- **Strict no-partial debit:** `billing/credit.py` rejects when `original_balance < amount_usdc`, never partially settling.
- **In-transaction spending limit:** `billing/credit.py` checks `daily_spend + amount_usdc > spending_limit` inside the locked transaction.
- **Pause gate:** `is_paused` blocks billable runs in `debit_credit`.
- **Top-up replay protection:** `billing/x402.py` `credit_usdc_topup` uses `ON CONFLICT (tx_hash) DO NOTHING`.
- **Atomic BIGINT USDC:** `migrations/versions/004_credits.sql` stores `balance_usdc BIGINT` with `CHECK (balance_usdc >= 0)`; `migrations/versions/013_settlement_retry.sql` and `014_org_spending_limits.sql` preserve the BIGINT convention.
- **Daily spend cache invalidation:** `billing/credit.py` invalidates the daily spend cache after each successful debit.
- **Immutable audit trail:** `org_credit_ledger` records every topup/debit with `balance_usdc_after`.

## Uncertainties and alternatives

- **Stripe webhook idempotency:** The manifest states "Stripe webhook processing must remain idempotent and retriable on transactional failure" as an invariant, but the actual `billing/stripe.py` implementation is not shown. Cannot verify whether double-settlement or lost-settlement protections exist there.
- **Settlement status update boundary:** The exact transaction boundary between `debit_credit` commit and `pending_settlements` status update is not shown in the manifest. This determines whether the double-debit crash window in Finding 2 is real.
- **Unique constraints on `pending_settlements`:** The manifest shows indexes on `next_retry_at` and `status`, but no unique constraint on `run_id` or a settlement-idempotency key. Without this, retry deduplication is unverified.
- **x402 reconciliation:** No reconciliation job for stranded x402 settlements is visible in the manifest; it may exist in code not included.
- **Daily spend cache staleness on refunds/top-ups:** The cache is invalidated on debit; whether refund or top-up paths invalidate it is not shown, which could cause over-rejection (not loss).

## Recommended follow-up tests or actions

1. **Verify settlement transaction boundaries:** Inspect `billing/settlement.py` `process_pending_settlements` to confirm whether the `pending_settlements` status update is in the same DB transaction as `debit_credit`. If not, add a unique constraint on `(run_id)` or an idempotency key to `org_credit_ledger.reason` to prevent double-debit on retry.
2. **Add x402 settlement reconciliation:** Implement a reconciliation job that matches on-chain tx hashes against credited records, so a caller who paid on-chain but whose credit write failed can be credited. Alternatively, allow retry of x402 settlements with idempotency.
3. **Test crash-window double-debit:** Write a fault-injection test that kills the worker between `debit_credit` commit and settlement-status update, then verify the retry does not double-debit.
4. **Review Stripe webhook idempotency:** Confirm `billing/stripe.py` uses the Stripe event id as a unique key and that webhook processing is retriable without double-settlement.
5. **Audit top-up credit-write failure:** Test `verify_and_settle_usdc_topup` with a simulated DB failure after on-chain settlement to confirm whether the credit is eventually applied or lost.
6. **Check cache invalidation on refunds/top-ups:** Verify all credit mutations (refund, top-up, onboarding grant) invalidate the daily spend cache to avoid stale-limit over-rejection.

## Sources

- `billing/credit.py` — credit verification, `SELECT FOR UPDATE` debit, strict no-partial debit, daily spend cache invalidation, ledger inserts.
- `billing/settlement.py` — retry worker logic; credit retry via `debit_credit`, x402 retry refusal.
- `billing/x402.py` — `verify_and_settle_usdc_topup`, `credit_usdc_topup` with `ON CONFLICT (tx_hash) DO NOTHING`, payment nonce cleanup.
- `billing/__init__.py` — billing facade, settlement routing, top-up requirements.
- `migrations/versions/004_credits.sql` — `org_credits` BIGINT atomic USDC schema and invariant.
- `migrations/versions/013_settlement_retry.sql` — pending settlements retry queue and indexes.
- `migrations/versions/014_org_spending_limits.sql` — spending limit and pause columns.
- `.github/skills/teardrop-domain-invariants/SKILL.md` — non-negotiable invariants (atomic USDC, auth_method/billing_method routing, SSRF, Stripe idempotency, cache invalidation).
