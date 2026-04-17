-- 025: Per-org CDP-backed agent wallets
-- Enables organisations to hold USDC via Coinbase Developer Platform managed wallets
-- for A2A delegation payments and MCP marketplace earnings.

CREATE TABLE IF NOT EXISTS org_agent_wallets (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    address          TEXT NOT NULL,
    cdp_account_name TEXT NOT NULL,
    chain_id         INTEGER NOT NULL DEFAULT 84532,
    wallet_type      TEXT NOT NULL DEFAULT 'eoa' CHECK (wallet_type IN ('eoa', 'smart_account')),
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, chain_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_wallets_org ON org_agent_wallets (org_id);
CREATE INDEX IF NOT EXISTS idx_agent_wallets_address ON org_agent_wallets (address);

CREATE TABLE IF NOT EXISTS agent_wallet_events (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    wallet_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('created', 'funded', 'withdrawn', 'deactivated')),
    amount_usdc BIGINT DEFAULT 0,
    detail      JSONB DEFAULT '{}',
    actor_id    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_wallet_events_org ON agent_wallet_events (org_id, created_at);
