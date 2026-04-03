-- Migration 004: org credit ledger
-- Adds a prepaid USDC credit balance per organisation, used by non-SIWE callers
-- (client_credentials, email) as an alternative to per-request x402 payments.
--
-- balance_usdc is stored as atomic USDC (6-decimal integer):
--   1_000_000 = $1.00,  10_000 = $0.01
--
-- Debit flow: billing gate checks balance >= run_price_usdc before the run;
--             debit_credit() debits the actual cost_usdc after the run.
-- Top-up flow: admin calls POST /admin/credits/topup (upsert).

CREATE TABLE IF NOT EXISTS org_credits (
    org_id       TEXT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    balance_usdc BIGINT NOT NULL DEFAULT 0 CHECK (balance_usdc >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_credits_org ON org_credits (org_id);
