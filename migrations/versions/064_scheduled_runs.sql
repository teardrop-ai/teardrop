CREATE TABLE IF NOT EXISTS scheduled_runs (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_kind TEXT NOT NULL DEFAULT 'interval',
    interval_seconds INTEGER NOT NULL,
    cron_expr TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    callback_url TEXT,
    next_run_at TIMESTAMPTZ NOT NULL,
    last_run_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT scheduled_runs_kind_chk CHECK (schedule_kind IN ('interval')),
    CONSTRAINT scheduled_runs_interval_chk CHECK (interval_seconds > 0)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_runs_org_created_at ON scheduled_runs (org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_due ON scheduled_runs (next_run_at, id) WHERE enabled = TRUE;

CREATE TABLE IF NOT EXISTS scheduled_run_results (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES scheduled_runs(id) ON DELETE CASCADE,
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    output_text TEXT NOT NULL DEFAULT '',
    cost_usdc BIGINT NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT scheduled_run_results_status_chk CHECK (status IN ('completed', 'failed', 'timeout', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_scheduled_run_results_schedule_created_at
    ON scheduled_run_results (schedule_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_scheduled_run_results_org_created_at
    ON scheduled_run_results (org_id, created_at DESC);