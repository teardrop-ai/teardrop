-- Migration 013: MCP Marketplace
-- Adds slug to orgs, publishing flags to org_tools, and marketplace
-- revenue-share tables (author config, earnings ledger, withdrawals).
--
-- All USDC amounts use atomic units (6-decimal integer): 1_000_000 = $1.00.
-- Revenue share stored as basis points (bps): 7000 = 70%.

-- ── Org slugs (namespace for published tools) ─────────────────────────────────
-- slug derived from org name; used in marketplace tool names ({slug}/{tool}).
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS slug TEXT;

-- Back-fill existing rows: lower-case, replace non-alphanumeric with hyphens,
-- trim leading/trailing hyphens, truncate to 40 chars.  The UPDATE is
-- idempotent — re-running won't change already-set slugs.
UPDATE orgs
SET slug = LEFT(
    TRIM(BOTH '-' FROM
        REGEXP_REPLACE(LOWER(name), '[^a-z0-9]+', '-', 'g')
    ), 40)
WHERE slug IS NULL;

-- Now enforce NOT NULL + UNIQUE.
ALTER TABLE orgs ALTER COLUMN slug SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orgs_slug ON orgs (slug);

-- ── Org tools: marketplace publishing flags ───────────────────────────────────
ALTER TABLE org_tools ADD COLUMN IF NOT EXISTS publish_as_mcp BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE org_tools ADD COLUMN IF NOT EXISTS marketplace_description TEXT NOT NULL DEFAULT '';

-- Partial index for fast marketplace catalog queries.
CREATE INDEX IF NOT EXISTS idx_org_tools_marketplace
    ON org_tools (name) WHERE publish_as_mcp = TRUE AND is_active = TRUE;

-- ── Tool author configuration ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tool_author_config (
    org_id              TEXT        PRIMARY KEY REFERENCES orgs(id),
    settlement_wallet   TEXT        NOT NULL,
    revenue_share_bps   INTEGER     NOT NULL DEFAULT 7000
                                    CHECK (revenue_share_bps >= 0 AND revenue_share_bps <= 10000),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Tool author earnings ledger (immutable per-call records) ──────────────────
CREATE TABLE IF NOT EXISTS tool_author_earnings (
    id                  TEXT        PRIMARY KEY,
    org_id              TEXT        NOT NULL REFERENCES orgs(id),
    tool_name           TEXT        NOT NULL,
    caller_org_id       TEXT        NOT NULL,
    amount_usdc         BIGINT      NOT NULL CHECK (amount_usdc >= 0),
    author_share_usdc   BIGINT      NOT NULL CHECK (author_share_usdc >= 0),
    platform_share_usdc BIGINT      NOT NULL CHECK (platform_share_usdc >= 0),
    status              TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'settled', 'failed')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_author_earnings_org_status
    ON tool_author_earnings (org_id, status);
CREATE INDEX IF NOT EXISTS idx_author_earnings_org_created
    ON tool_author_earnings (org_id, created_at DESC);

-- ── Tool author withdrawal requests ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tool_author_withdrawals (
    id                  TEXT        PRIMARY KEY,
    org_id              TEXT        NOT NULL REFERENCES orgs(id),
    amount_usdc         BIGINT      NOT NULL CHECK (amount_usdc > 0),
    tx_hash             TEXT        NOT NULL DEFAULT '',
    wallet              TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'settled', 'failed')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_author_withdrawals_org
    ON tool_author_withdrawals (org_id, created_at DESC);
