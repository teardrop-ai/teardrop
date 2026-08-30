-- Migration 099: optional 24-hour credit spend limits for principals within an org
-- Domain: billing
-- Invariant: daily_limit_usdc is BIGINT atomic USDC; limits are advisory caps enforced at the billing gate, never mutate ledgers
--
-- Lets orgs cap how much a single principal can spend from the org credit
-- balance per rolling 24h window. is_paused mirrors the org-level pause
-- semantics. Enforcement lives in the billing gate; this table is data only.

CREATE TABLE IF NOT EXISTS org_principal_spend_limits (
    org_id           TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    principal_id     TEXT NOT NULL,
    daily_limit_usdc BIGINT NOT NULL CHECK (daily_limit_usdc > 0),
    is_paused        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, principal_id)
);