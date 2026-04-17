-- Migration 024: A2A delegation billing — per-agent cost caps and delegation event ledger
--
-- Adds billing-related columns to a2a_allowed_agents so orgs can control
-- per-agent spend caps and opt in to x402 payment mode.
--
-- Creates a2a_delegation_events table to record every outbound delegation
-- with cost, status, and settlement details for auditability.

-- ── Extend a2a_allowed_agents with billing controls ──────────────────────────

ALTER TABLE a2a_allowed_agents
    ADD COLUMN IF NOT EXISTS max_cost_usdc BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS require_x402  BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN a2a_allowed_agents.max_cost_usdc IS
    'Per-delegation cost cap in atomic USDC (6 decimals). 0 = use global default.';
COMMENT ON COLUMN a2a_allowed_agents.require_x402 IS
    'When TRUE, outbound calls to this agent must use x402 payment headers.';

-- ── A2A delegation event ledger ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS a2a_delegation_events (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL,
    agent_url       TEXT NOT NULL,
    agent_name      TEXT NOT NULL DEFAULT '',
    task_status     TEXT NOT NULL DEFAULT 'pending',
    cost_usdc       BIGINT NOT NULL DEFAULT 0,
    billing_method  TEXT NOT NULL DEFAULT 'credit',
    settlement_tx   TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_events_org
    ON a2a_delegation_events (org_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_events_run
    ON a2a_delegation_events (run_id);
