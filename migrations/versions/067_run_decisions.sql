-- Migration 067: per-run decision ledger (decision graph foundation)
-- Domain: agent memory / ML data foundation
-- Invariant: Additive, non-financial telemetry; must never gate or block billing/settlement paths
--
-- Persists one structured decision summary per agent run (action taken,
-- reasoning, task classification, confidence, allowlisted slot snapshot, and
-- the tools used) alongside the unstructured facts already stored in
-- org_memories. Extends the outcome-labeling foundation introduced by
-- run_feedback (migration 066) with an explicit outcome column so a rating
-- can be attributed back to the specific decision it evaluates.
--
-- slots_snapshot only ever contains allowlisted keys (see
-- teardrop/memory.py _SLOTS_SNAPSHOT_ALLOWLIST) — raw tool call arguments
-- (wallet addresses, API keys, etc.) are never persisted here.

CREATE TABLE IF NOT EXISTS run_decisions (
    id              TEXT        PRIMARY KEY,
    run_id          TEXT        NOT NULL,
    org_id          TEXT        NOT NULL DEFAULT '',
    user_id         TEXT        NOT NULL DEFAULT '',
    task_class      TEXT        NOT NULL DEFAULT '',
    action          TEXT        NOT NULL DEFAULT '',
    reasoning       TEXT        NOT NULL DEFAULT '',
    confidence      NUMERIC,
    slots_snapshot  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    tool_names      TEXT[]      NOT NULL DEFAULT '{}',
    outcome         SMALLINT    NOT NULL DEFAULT 0 CHECK (outcome IN (-1, 0, 1)),
    outcome_source  TEXT        NOT NULL DEFAULT '',
    outcome_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT run_decisions_run_id_unique UNIQUE (run_id)
);

CREATE INDEX IF NOT EXISTS idx_run_decisions_org_created
    ON run_decisions (org_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_run_decisions_outcome
    ON run_decisions (outcome, created_at DESC)
    WHERE outcome != 0;

COMMENT ON TABLE run_decisions IS
    'One structured decision summary per agent run (action, reasoning, tools used, outcome label) -- the decision-graph foundation for outcome-linked tool reputation and future routing classifiers. Non-financial -- never used for billing.';
