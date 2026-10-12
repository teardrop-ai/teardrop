-- Migration 127: marketplace author earnings carried on credit settlement retries.
-- Domain: billing / marketplace
-- Invariant: additive. Each element is {author_org_id, caller_org_id, tool_name,
-- total_cost_usdc} with BIGINT-range atomic USDC integers. The retry worker inserts
-- the matching tool_author_earnings rows in the same transaction as the recovered
-- credit debit, so authors are paid exactly once. Existing rows default to no
-- allocations; no rows are rewritten.

ALTER TABLE pending_settlements
    ADD COLUMN IF NOT EXISTS earnings_allocations JSONB NOT NULL DEFAULT '[]'::jsonb;
