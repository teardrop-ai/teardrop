-- Migration 066: per-tool-call telemetry ledger + reputation foundation
-- Domain: observability / ML data foundation / marketplace
-- Invariant: Additive, non-financial telemetry; must never gate or block billing/settlement paths
--
-- Persists per-call execution telemetry (latency, success/failure, structured
-- error class) that agent/node_executor.py already computes in-memory every
-- run but previously discarded once the run ended. This is the foundation for
-- future ML classifiers (failure prediction, tool quality scoring) and for the
-- marketplace reputation rollup (see reputation_rollup_once in marketplace/worker.py).
--
-- Never stores raw tool arguments (may contain wallet addresses/secrets) —
-- only the truncated SHA-256 dedup hash already computed for within-run
-- deduplication (agent/node_executor.py _call_signature).

CREATE TABLE IF NOT EXISTS tool_call_events (
    id          TEXT        PRIMARY KEY,
    run_id      TEXT        NOT NULL DEFAULT '',
    org_id      TEXT        NOT NULL DEFAULT '',
    tool_name   TEXT        NOT NULL,
    success     BOOLEAN     NOT NULL DEFAULT TRUE,
    error_class TEXT        NOT NULL DEFAULT '',
    elapsed_ms  INTEGER     NOT NULL DEFAULT 0,
    billable    BOOLEAN     NOT NULL DEFAULT TRUE,
    args_hash   TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tool_call_events_tool_created
    ON tool_call_events (tool_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tool_call_events_error_created
    ON tool_call_events (error_class, created_at DESC)
    WHERE error_class != '';

CREATE INDEX IF NOT EXISTS idx_tool_call_events_org_created
    ON tool_call_events (org_id, created_at DESC)
    WHERE org_id != '';

CREATE INDEX IF NOT EXISTS idx_tool_call_events_run
    ON tool_call_events (run_id)
    WHERE run_id != '';

COMMENT ON TABLE tool_call_events IS
    'Per-tool-call telemetry (latency, success, error taxonomy) for ML training data and marketplace reputation rollups. Non-financial -- never used for billing.';

-- ── Marketplace reputation aggregates (extends 058_marketplace_dashboard_catalog.sql) ──
-- total_failures / total_latency_ms / reputation_score are recomputed wholesale
-- (SET, not incremented) by reputation_rollup_once() from tool_call_events, so
-- the rollup stays idempotent. total_calls remains owned exclusively by
-- record_marketplace_tool_call() (marketplace/stats.py) and is never touched here.
ALTER TABLE marketplace_tool_call_stats ADD COLUMN IF NOT EXISTS total_failures BIGINT NOT NULL DEFAULT 0 CHECK (total_failures >= 0);
ALTER TABLE marketplace_tool_call_stats ADD COLUMN IF NOT EXISTS total_latency_ms BIGINT NOT NULL DEFAULT 0 CHECK (total_latency_ms >= 0);
ALTER TABLE marketplace_tool_call_stats ADD COLUMN IF NOT EXISTS reputation_score NUMERIC NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_marketplace_tool_call_stats_reputation
    ON marketplace_tool_call_stats (reputation_score DESC, qualified_tool_name ASC);

-- ── User/agent feedback ledger (ground-truth labels for future classifiers) ──
CREATE TABLE IF NOT EXISTS run_feedback (
    id                  TEXT        PRIMARY KEY,
    run_id              TEXT        NOT NULL,
    org_id              TEXT        NOT NULL DEFAULT '',
    user_id             TEXT        NOT NULL DEFAULT '',
    qualified_tool_name TEXT        NOT NULL DEFAULT '',
    rating              SMALLINT    NOT NULL CHECK (rating IN (-1, 0, 1)),
    comment             TEXT        NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_run_feedback_run ON run_feedback (run_id);
CREATE INDEX IF NOT EXISTS idx_run_feedback_org_created ON run_feedback (org_id, created_at DESC) WHERE org_id != '';
CREATE INDEX IF NOT EXISTS idx_run_feedback_tool_created ON run_feedback (qualified_tool_name, created_at DESC) WHERE qualified_tool_name != '';
