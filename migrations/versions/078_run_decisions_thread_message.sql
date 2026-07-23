-- Migration 078: Add thread_id and user_message to run_decisions for implicit correction detection.
-- Domain: agent telemetry / ML data foundation
-- Invariant: Additive columns only; never referenced by billing or settlement paths.
-- user_message stores only the first 200 characters of the human request (no full prompts).
-- thread_id enables per-thread lookups so follow-up turns can inform prior outcome labels.

ALTER TABLE run_decisions
    ADD COLUMN IF NOT EXISTS thread_id TEXT NOT NULL DEFAULT '';

ALTER TABLE run_decisions
    ADD COLUMN IF NOT EXISTS user_message TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_run_decisions_thread_created
    ON run_decisions (org_id, thread_id, created_at DESC)
    WHERE thread_id != '';

COMMENT ON COLUMN run_decisions.thread_id IS
    'Scoped thread identifier (user_id:thread_id) for implicit correction lookups.';

COMMENT ON COLUMN run_decisions.user_message IS
    'First 200 chars of the human user message at run time. Truncated to avoid storing raw prompts. Never contains credentials.';