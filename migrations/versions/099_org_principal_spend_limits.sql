-- 099: Optional 24-hour credit spend limits for principals within an org.

CREATE TABLE IF NOT EXISTS org_principal_spend_limits (
    org_id           TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    principal_id     TEXT NOT NULL,
    daily_limit_usdc BIGINT NOT NULL CHECK (daily_limit_usdc > 0),
    is_paused        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, principal_id)
);