-- Migration 068: persisted per-org tool exclusions
-- Domain: agent tool policy
-- Invariant: Additive-only, advisory data; never referenced by billing/settlement.
-- Merged with the per-request ToolPolicy.exclude_names (teardrop/agent_schemas.py)
-- before entering agent state -- see teardrop/tool_exclusions.py.
--
-- Backs a durable "hide this tool from my org's agent" dashboard preference.
-- Tool names are stored pre-normalized (no platform/ or org/ prefix), matching
-- the internal executor/binder keys produced by _normalize_exclusion_name so
-- no normalization is needed on the read path used by agent runs.

CREATE TABLE IF NOT EXISTS org_tool_exclusions (
    org_id     TEXT        NOT NULL,
    tool_name  TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, tool_name)
);

CREATE INDEX IF NOT EXISTS idx_org_tool_exclusions_org
    ON org_tool_exclusions (org_id);
