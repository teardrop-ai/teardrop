-- Squashed migration 102_squashed_baseline; source order is preserved.

-- Source migration: 001_baseline
-- Migration 001: baseline schema
-- Domain: auth
-- Invariant: Core tables (orgs, users, usage_events, wallets, SIWE nonces); idempotent via IF NOT EXISTS
-- Creates all core tables that are also created imperatively by init_*_db().
-- Idempotent via IF NOT EXISTS — safe to run against an existing Neon database.

-- ── Organisations ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orgs (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL
);

-- ── Users ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    hashed_secret TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL
);

-- ── Usage events ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS usage_events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    org_id      TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    tool_calls  INTEGER NOT NULL DEFAULT 0,
    tool_names  TEXT NOT NULL DEFAULT '[]',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_org  ON usage_events (org_id, created_at);

-- ── Wallets ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallets (
    id         TEXT PRIMARY KEY,
    address    TEXT NOT NULL,
    chain_id   INTEGER NOT NULL,
    user_id    TEXT NOT NULL REFERENCES users(id),
    org_id     TEXT NOT NULL REFERENCES orgs(id),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (address, chain_id)
);

CREATE INDEX IF NOT EXISTS idx_wallets_user ON wallets (user_id);

-- ── SIWE nonces ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS siwe_nonces (
    nonce      TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT FALSE
);

-- ── LangGraph checkpoints ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id           TEXT   NOT NULL,
    checkpoint_ns       TEXT   NOT NULL DEFAULT '',
    checkpoint_id       TEXT   NOT NULL,
    parent_checkpoint_id TEXT,
    type                TEXT,
    checkpoint          JSONB  NOT NULL,
    metadata            JSONB  NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id     TEXT   NOT NULL,
    checkpoint_ns TEXT   NOT NULL DEFAULT '',
    checkpoint_id TEXT   NOT NULL,
    task_id       TEXT   NOT NULL,
    idx           INTEGER NOT NULL,
    channel       TEXT   NOT NULL,
    type          TEXT,
    blob          BYTEA  NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- Source migration: 002_billing
-- Migration 002: billing schema
-- Domain: billing
-- Invariant: usage_events cost/settlement columns + pricing_rules; USDC stored as BIGINT atomic units
-- Extends usage_events with settlement columns and adds pricing_rules table.

-- ── Extend usage_events ───────────────────────────────────────────────────────
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_usdc BIGINT NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS settlement_tx TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS settlement_status TEXT NOT NULL DEFAULT 'none';

CREATE INDEX IF NOT EXISTS idx_usage_settlement
    ON usage_events (settlement_status, created_at)
    WHERE settlement_status != 'none';

-- ── Pricing rules ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pricing_rules (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    run_price_usdc        BIGINT NOT NULL DEFAULT 10000,   -- $0.01 in 6-decimal atomic units
    tokens_in_cost_per_1k BIGINT NOT NULL DEFAULT 0,       -- reserved for upto scheme
    tokens_out_cost_per_1k BIGINT NOT NULL DEFAULT 0,      -- reserved for upto scheme
    tool_call_cost        BIGINT NOT NULL DEFAULT 0,       -- reserved for upto scheme
    effective_from        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed default pricing rule
INSERT INTO pricing_rules (id, name, run_price_usdc)
VALUES ('default', 'Default pricing', 10000)
ON CONFLICT (id) DO NOTHING;

-- Source migration: 003_pricing_seed
-- Migration 003: usage-based pricing rule
-- Domain: billing
-- Invariant: Per-unit rates in atomic USDC (6-decimal BIGINT); 1_000_000 = $1.00
-- Adds a usage-based pricing rule alongside the existing flat-rate default.
-- Per-unit rates are in atomic USDC (6-decimal integer):
--   1_000_000 = $1.00,  10_000 = $0.01,  1_000 = $0.001
--
-- These starter rates approximate wholesale LLM cost at a small markup:
--   tokens_in_cost_per_1k  = 1500  → $0.0015 per 1k input tokens
--   tokens_out_cost_per_1k = 7500  → $0.0075 per 1k output tokens
--   tool_call_cost         = 1000  → $0.001  per tool invocation
--   run_price_usdc         = 10000 → $0.01   (minimum floor, unused by this rule)
--
-- calculate_run_cost_usdc() uses per-unit rates when they are non-zero;
-- falls back to run_price_usdc for flat-rate rules (where all per-unit = 0).

INSERT INTO pricing_rules (
    id,
    name,
    run_price_usdc,
    tokens_in_cost_per_1k,
    tokens_out_cost_per_1k,
    tool_call_cost,
    effective_from,
    created_at
)
VALUES (
    'usage-based-v1',
    'Usage-based pricing v1',
    10000,    -- $0.01 floor (not charged when per-unit rates apply)
    1500,     -- $0.0015 / 1k input tokens
    7500,     -- $0.0075 / 1k output tokens
    1000,     -- $0.001  / tool call
    NOW(),
    NOW()
)
ON CONFLICT (id) DO NOTHING;

-- Source migration: 004_credits
-- Migration 004: org credit ledger
-- Domain: billing
-- Invariant: balance_usdc is BIGINT atomic USDC; prepaid balance for non-SIWE callers
-- Adds a prepaid USDC credit balance per organisation, used by non-SIWE callers
-- (client_credentials, email) as an alternative to per-request x402 payments.
--
-- balance_usdc is stored as atomic USDC (6-decimal integer):
--   1_000_000 = $1.00,  10_000 = $0.01
--
-- Debit flow: billing gate checks balance >= run_price_usdc before the run;
--             debit_credit() debits the actual cost_usdc after the run.
-- Top-up flow: admin calls POST /admin/credits/topup (upsert).

CREATE TABLE IF NOT EXISTS org_credits (
    org_id       TEXT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    balance_usdc BIGINT NOT NULL DEFAULT 0 CHECK (balance_usdc >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_credits_org ON org_credits (org_id);

-- Source migration: 005_org_client_credentials
-- Migration 005: org-scoped M2M client credentials
-- Domain: auth
-- Invariant: Client secrets stored only as PBKDF2-SHA256 hashes; plaintext returned once at creation
-- Allows per-org machine-to-machine credentials stored in the database,
-- separate from the environment-variable-based fallback credential.
--
-- Secrets are stored as PBKDF2-SHA256 hashes (same scheme as users.salt/hashed_secret).
-- The plaintext client_secret is returned to the caller exactly once at creation time.

CREATE TABLE IF NOT EXISTS org_client_credentials (
    client_id     TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    hashed_secret TEXT NOT NULL,
    salt          TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_client_creds_org ON org_client_credentials (org_id);

-- Source migration: 006_credit_ledger
-- Migration 006: org credit ledger
-- Domain: billing
-- Invariant: Append-only audit trail; one immutable row per debit/top-up
-- Adds an immutable audit trail for all credit operations (debits and top-ups).
--
-- Every debit_credit() and admin_topup_credit() call inserts one row.
-- balance_usdc_after captures the post-operation balance for easy reconciliation.

CREATE TABLE IF NOT EXISTS org_credit_ledger (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    operation          TEXT NOT NULL CHECK (operation IN ('debit', 'topup')),
    amount_usdc        BIGINT NOT NULL CHECK (amount_usdc > 0),
    balance_usdc_after BIGINT NOT NULL CHECK (balance_usdc_after >= 0),
    reason             TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_credit_ledger_org_created
    ON org_credit_ledger (org_id, created_at DESC);

-- Source migration: 007_stripe_webhook_events
-- Migration 007: Stripe webhook events (idempotency + audit)
-- Domain: billing
-- Invariant: PRIMARY KEY on stripe_event_id prevents double-topup on webhook replay
-- Stores each processed checkout.session.completed event exactly once.
-- The PRIMARY KEY on stripe_event_id is the idempotency guard:
-- duplicate webhook deliveries → ON CONFLICT DO NOTHING → no double-topup.

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    stripe_event_id  TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    amount_usdc      BIGINT NOT NULL CHECK (amount_usdc > 0),
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stripe_events_org
    ON stripe_webhook_events (org_id, processed_at DESC);

-- Source migration: 008_usdc_topup_events
-- Migration 008: USDC on-chain top-up events (idempotency + audit)
-- Domain: billing
-- Invariant: PRIMARY KEY on tx_hash prevents double-credit on duplicate tx submission
-- Stores each settled USDC top-up transaction exactly once.
-- The PRIMARY KEY on tx_hash is the idempotency guard:
-- duplicate submissions of the same on-chain tx → ON CONFLICT DO NOTHING → no double-credit.

CREATE TABLE IF NOT EXISTS usdc_topup_events (
    tx_hash      TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    amount_usdc  BIGINT NOT NULL CHECK (amount_usdc > 0),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usdc_topup_events_org
    ON usdc_topup_events (org_id, processed_at DESC);

-- Source migration: 009_a2a_delegation
-- Migration 009: A2A delegation allowlist (per-org trusted remote agents)
-- Domain: delegation
-- Invariant: UNIQUE per org prevents duplicate allowlist entries; only allowlisted agents receive delegations
-- Each row authorises an org to delegate tasks to a specific A2A agent URL.
-- The UNIQUE constraint prevents duplicate entries per org.

CREATE TABLE IF NOT EXISTS a2a_allowed_agents (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    agent_url  TEXT NOT NULL,
    label      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, agent_url)
);

CREATE INDEX IF NOT EXISTS idx_a2a_allowed_agents_org
    ON a2a_allowed_agents (org_id);

-- Source migration: 009_tool_pricing_overrides
-- Migration 009: per-tool pricing overrides
-- Domain: billing
-- Invariant: cost_usdc is BIGINT atomic USDC; overrides the flat tool_call_cost
-- Adds tool_pricing_overrides table that lets admins set a custom cost_usdc
-- for individual tools, overriding the flat tool_call_cost from pricing_rules.
--
-- cost_usdc uses the same atomic USDC convention (6-decimal integer):
--   1_000_000 = $1.00,  15_000 = $0.015,  2_000 = $0.002,  1_000 = $0.001

CREATE TABLE IF NOT EXISTS tool_pricing_overrides (
    tool_name   TEXT        PRIMARY KEY,
    cost_usdc   BIGINT      NOT NULL CHECK (cost_usdc >= 0),
    description TEXT        NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed initial overrides for tools whose COGS exceed the flat tool_call_cost.
--   web_search:         $0.015  (Tavily costs $0.008/call; 15000 gives ~47% margin)
--   get_token_price:    $0.002  (CoinGecko overage $0.0005/call; 2× premium for market data)
--   get_wallet_portfolio: $0.005 (value pricing — replaces 5–15 individual calls)

INSERT INTO tool_pricing_overrides (tool_name, cost_usdc, description)
VALUES
    ('web_search',           15000, 'Tavily search API — covers COGS with margin'),
    ('get_token_price',       2000, 'CoinGecko market data — premium for external API dependency'),
    ('get_wallet_portfolio',  5000, 'Value pricing — aggregates multiple on-chain queries')
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 010_org_tools
-- Migration 010: per-org custom tools
-- Domain: tools
-- Invariant: Custom tools are org-scoped; never exposed in the public A2A card or MCP server
-- Allows organisations to register webhook-backed tools that are injected
-- into the agent at run time.  Tools are org-scoped and never appear in
-- the public A2A agent card or MCP server.

-- ── Custom tool definitions ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS org_tools (
    id               TEXT        PRIMARY KEY,
    org_id           TEXT        NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name             TEXT        NOT NULL,
    description      TEXT        NOT NULL DEFAULT '',
    input_schema     JSONB       NOT NULL,
    webhook_url      TEXT        NOT NULL,
    webhook_method   TEXT        NOT NULL DEFAULT 'POST'
                                 CHECK (webhook_method IN ('GET', 'POST', 'PUT')),
    auth_header_name TEXT,
    auth_header_enc  TEXT,
    timeout_seconds  INTEGER     NOT NULL DEFAULT 10
                                 CHECK (timeout_seconds BETWEEN 1 AND 30),
    is_active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, name)
);

CREATE INDEX IF NOT EXISTS idx_org_tools_org_active
    ON org_tools (org_id) WHERE is_active = TRUE;

-- ── Immutable audit trail ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS org_tool_events (
    id           TEXT        PRIMARY KEY,
    org_id       TEXT        NOT NULL,
    tool_id      TEXT        NOT NULL,
    tool_name    TEXT        NOT NULL,
    event_type   TEXT        NOT NULL
                             CHECK (event_type IN ('created', 'updated', 'deleted', 'executed', 'failed')),
    actor_id     TEXT        NOT NULL,
    detail       JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_tool_events_org_created
    ON org_tool_events (org_id, created_at DESC);

-- Source migration: 011_org_memories
-- Migration 011: org memories (persistent agent memory / RAG per org)
-- Domain: memory
-- Invariant: Memories are org-scoped; recall must filter by org_id
-- Enables pgvector extension and creates org_memories table for storing
-- per-org embedding-backed factual memories recalled during agent runs.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS org_memories (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    content       TEXT NOT NULL CHECK (length(content) <= 500),
    embedding     VECTOR(1536) NOT NULL,
    source_run_id TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for fast cosine similarity search within an org.
CREATE INDEX IF NOT EXISTS idx_org_memories_embedding
    ON org_memories USING hnsw (embedding vector_cosine_ops);

-- Composite index for listing / pagination by org.
CREATE INDEX IF NOT EXISTS idx_org_memories_org_created
    ON org_memories (org_id, created_at DESC);

-- Source migration: 012_org_mcp_servers
-- Migration 012: Per-org MCP server connections
-- Domain: tools
-- Invariant: MCP server connections are org-scoped
-- Allows organisations to register external MCP servers whose tools are
-- dynamically discovered and injected into the agent at run time.
--
-- Pattern: mirrors org_tools + org_tool_events from migration 010.

CREATE TABLE IF NOT EXISTS org_mcp_servers (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    url              TEXT NOT NULL,
    auth_type        TEXT NOT NULL DEFAULT 'none'
                     CHECK (auth_type IN ('none', 'bearer', 'header')),
    auth_token_enc   TEXT,
    auth_header_name TEXT,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    timeout_seconds  INTEGER NOT NULL DEFAULT 15
                     CHECK (timeout_seconds BETWEEN 1 AND 60),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, name)
);

CREATE INDEX IF NOT EXISTS idx_org_mcp_servers_org
    ON org_mcp_servers (org_id) WHERE is_active;

-- Immutable audit trail for MCP server lifecycle events.
CREATE TABLE IF NOT EXISTS org_mcp_server_events (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    server_id   TEXT NOT NULL,
    server_name TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL CHECK (event_type IN (
        'created', 'updated', 'deleted',
        'connected', 'connection_failed'
    )),
    detail      TEXT NOT NULL DEFAULT '',
    actor_id    TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_mcp_server_events_org_created
    ON org_mcp_server_events (org_id, created_at DESC);

-- Source migration: 013_mcp_marketplace
-- Migration 013: MCP Marketplace
-- Domain: marketplace
-- Invariant: USDC amounts BIGINT atomic; revenue share in bps (7000 = 70% author)
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

-- Source migration: 013_settlement_retry
-- Migration 013: pending settlements retry queue
-- Domain: billing
-- Invariant: Failed settlements retried with exponential backoff; amounts BIGINT atomic USDC
-- Stores failed settlements for asynchronous retry with exponential backoff.
-- Both x402 on-chain and credit debit failures are enqueued here.

CREATE TABLE IF NOT EXISTS pending_settlements (
    id                TEXT PRIMARY KEY,
    usage_event_id    TEXT NOT NULL,
    org_id            TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    billing_method    TEXT NOT NULL CHECK (billing_method IN ('x402', 'credit')),
    amount_usdc       BIGINT NOT NULL DEFAULT 0,
    payment_payload   TEXT,           -- base64-encoded for x402; NULL for credit
    retry_count       INT NOT NULL DEFAULT 0,
    max_retries       INT NOT NULL DEFAULT 5,
    next_retry_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error        TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'retrying', 'settled', 'exhausted')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The retry worker queries this index every N seconds.
CREATE INDEX IF NOT EXISTS idx_pending_settlements_retry
    ON pending_settlements (next_retry_at)
    WHERE status IN ('pending', 'retrying');

-- Admin reconciliation: quickly find exhausted items.
CREATE INDEX IF NOT EXISTS idx_pending_settlements_status
    ON pending_settlements (status, created_at DESC);

-- Source migration: 014_org_spending_limits
-- Migration 014: org spending limits + admin pause
-- Domain: billing
-- Invariant: spending_limit_usdc is a 24h rolling cap; is_paused blocks billable runs
-- Adds spending_limit_usdc (24h rolling window cap) and is_paused flag
-- to org_credits. Default values preserve existing behaviour.

ALTER TABLE org_credits
    ADD COLUMN IF NOT EXISTS spending_limit_usdc BIGINT NOT NULL DEFAULT 0;
    -- 0 = unlimited (no cap enforced)

ALTER TABLE org_credits
    ADD COLUMN IF NOT EXISTS is_paused BOOLEAN NOT NULL DEFAULT FALSE;

-- Source migration: 015_memory_ttl_dedup
-- Migration 015: memory deduplication + TTL expiry
-- Domain: memory
-- Invariant: UNIQUE(org_id, content_hash) prevents duplicate facts per org
-- Adds content_hash for exact-match dedup and expires_at for TTL enforcement.

ALTER TABLE org_memories
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

ALTER TABLE org_memories
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- Unique index on (org_id, content_hash) prevents exact-duplicate facts per org.
-- WHERE content_hash IS NOT NULL: existing rows without a hash are ignored.
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_memories_dedup
    ON org_memories (org_id, content_hash)
    WHERE content_hash IS NOT NULL;

-- Backfill content_hash for existing rows.
UPDATE org_memories
SET content_hash = encode(sha256(lower(trim(content))::bytea), 'hex')
WHERE content_hash IS NULL;

-- Partial index for efficient expired-memory cleanup.
CREATE INDEX IF NOT EXISTS idx_org_memories_expires
    ON org_memories (expires_at)
    WHERE expires_at IS NOT NULL;

-- Source migration: 016_email_verification
-- Migration 016: email verification
-- Domain: auth
-- Invariant: Verification tokens are single-use and time-limited
-- Adds is_verified flag to existing users (TRUE = pre-verified for all
-- admin-created accounts) and creates the email_verification_tokens table
-- used by self-registered users.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT FALSE
);

-- Source migration: 017_org_invites
-- Migration 017: org member invites
-- Domain: auth
-- Invariant: Invite tokens are single-use and scoped to one org
-- Token-authenticated invitation flow for adding users to an existing org
-- without requiring a Teardrop platform admin.

CREATE TABLE IF NOT EXISTS org_invites (
    token      TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email      TEXT,
    role       TEXT NOT NULL DEFAULT 'user',
    invited_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_org_invites_org ON org_invites (org_id);

-- Source migration: 018_refresh_tokens
-- Migration 018: refresh tokens
-- Domain: auth
-- Invariant: Tokens rotated on every use (OWASP); reuse of a rotated token must be rejected
-- Long-lived tokens (default 30 days) exchanged for short-lived access tokens
-- (30 min). Tokens are rotated on every use per OWASP best practice.
-- Covers email and SIWE auth flows.

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token        TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id       TEXT NOT NULL,
    auth_method  TEXT NOT NULL,
    extra_claims JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens (user_id);

-- Source migration: 019_org_llm_config
-- Migration 019: per-org LLM configuration for multi-model gateway + BYOK support
-- Domain: auth
-- Invariant: BYOK API keys are org-scoped and must never be logged

CREATE TABLE IF NOT EXISTS org_llm_config (
    org_id              TEXT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL DEFAULT 'anthropic'
                        CHECK (provider IN ('anthropic', 'openai', 'google')),
    model               TEXT NOT NULL,
    api_key_enc         TEXT,
    api_base            TEXT,
    max_tokens          INTEGER NOT NULL DEFAULT 4096
                        CHECK (max_tokens BETWEEN 1 AND 200000),
    temperature         REAL NOT NULL DEFAULT 0.0
                        CHECK (temperature BETWEEN 0.0 AND 2.0),
    timeout_seconds     INTEGER NOT NULL DEFAULT 120
                        CHECK (timeout_seconds BETWEEN 10 AND 600),
    routing_preference  TEXT NOT NULL DEFAULT 'default'
                        CHECK (routing_preference IN ('default', 'cost', 'speed', 'quality')),
    is_byok             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Source migration: 020_usage_provider_model
-- Migration 020: track which provider/model was used for each agent run
-- Domain: billing
-- Invariant: Records provider/model per run for accurate per-model pricing

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_usage_events_provider_model
    ON usage_events (provider, model);

-- Source migration: 021_model_pricing
-- Migration 021: extend pricing_rules with provider/model columns for per-model pricing
-- Domain: billing
-- Invariant: Per-model rates stored in BIGINT atomic USDC

ALTER TABLE pricing_rules ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE pricing_rules ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_pricing_rules_provider_model
    ON pricing_rules (provider, model, effective_from DESC);

-- Source migration: 022_model_pricing_seed
-- Migration 022: seed model-specific pricing rules (Teardrop shared-key rates with ~25% margin)
-- Domain: billing
-- Invariant: Rates in BIGINT atomic USDC (6 decimals)
-- Rates in atomic USDC (6 decimals). E.g. 313 = $0.000313 per 1k tokens.

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('anthropic-haiku-v1', 'Claude Haiku 4.5', 'anthropic', 'claude-haiku-4-5-20251001',
     10000, 313, 1563, 1000, NOW()),
    ('openai-gpt4o-mini-v1', 'GPT-4o Mini', 'openai', 'gpt-4o-mini',
     10000, 188, 750, 1000, NOW()),
    ('google-flash-v1', 'Gemini 2.0 Flash', 'google', 'gemini-2.0-flash',
     10000, 94, 375, 1000, NOW())
ON CONFLICT (id) DO NOTHING;

-- Source migration: 023_siwe_login_sessions
-- Migration 023: SIWE QR login sessions
-- Domain: auth
-- Invariant: Nonce is single-use; access/refresh tokens populated only when status='complete'
-- SIWE QR login sessions for the CLI → browser → wallet sign flow.
--
-- A session ties a single-use nonce to a session_id so that:
--   1. The CLI can create the session (POST /auth/siwe/sessions) and poll for completion.
--   2. The browser signing page can read the nonce/domain (GET /auth/siwe/sessions/{id})
--      and submit the signed message (POST /auth/siwe/sessions/{id}/complete).
-- The access_token and refresh_token columns are populated only when status='complete'.

CREATE TABLE IF NOT EXISTS siwe_login_sessions (
    session_id    TEXT        PRIMARY KEY,
    nonce         TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending',  -- 'pending' | 'complete'
    access_token  TEXT,
    refresh_token TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL
);

-- Used by a periodic cleanup job and for expiry filtering on reads.
CREATE INDEX IF NOT EXISTS idx_siwe_login_sessions_expires
    ON siwe_login_sessions (expires_at);

-- Source migration: 024_a2a_delegation_billing
-- Migration 024: A2A delegation billing — per-agent cost caps and delegation event ledger
-- Domain: delegation
-- Invariant: Per-agent spend caps enforced; delegation events recorded with BIGINT atomic USDC cost
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

-- Source migration: 025_org_agent_wallets
-- 025: Per-org CDP-backed agent wallets
-- Domain: billing
-- Invariant: CDP wallet secrets are never stored in plaintext or logged
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

-- Source migration: 026_a2a_jwt_forward
-- 026: Add jwt_forward flag to a2a_allowed_agents
-- Domain: delegation
-- Invariant: Caller-JWT forwarding is per-agent opt-in only
-- Allows per-agent opt-in to forward caller JWT as Authorization header.

ALTER TABLE a2a_allowed_agents
    ADD COLUMN IF NOT EXISTS jwt_forward BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN a2a_allowed_agents.jwt_forward IS
    'When TRUE, forward caller JWT as Authorization header to this agent.';

-- Source migration: 026_normalize_revenue_share
-- Migration 026: Normalize revenue_share_bps to default
-- Domain: marketplace
-- Invariant: All authors use the platform default 7000 bps (70/30 author/platform split)
--
-- Resets all tool_author_config.revenue_share_bps values to the platform default (7000 = 70%).
-- The column is NOT dropped — it is preserved for future automatic volume-based tier systems.
--
-- Context: Per-author revenue_share_bps overrides are no longer supported by the application.
-- All authors now receive the fixed platform default (70/30 split). The code no longer reads
-- or writes revenue_share_bps, but the DB column is retained for forward compatibility.

UPDATE tool_author_config
SET revenue_share_bps = 7000
WHERE revenue_share_bps IS NOT NULL AND revenue_share_bps != 7000;

-- Source migration: 027_marketplace_tool_pricing
-- 027: Add author-controlled per-tool pricing to org_tools.
-- Domain: marketplace
-- Invariant: base_price_usdc is BIGINT atomic USDC; 0 = platform default; capped at $100.00
--
-- Allows tool authors to set a base price for each published tool.
-- 0 = use platform default pricing.  Max $100.00 (100_000_000 atomic USDC).

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS base_price_usdc BIGINT NOT NULL DEFAULT 0
    CHECK (base_price_usdc >= 0 AND base_price_usdc <= 100000000);

COMMENT ON COLUMN org_tools.base_price_usdc IS
    'Author-set per-call price in atomic USDC (6 decimals). 0 = platform default.';

-- Source migration: 028_marketplace_subscriptions
-- 028: Marketplace subscriptions — let orgs subscribe to marketplace tools
-- Domain: marketplace
-- Invariant: Subscriptions keyed by qualified name (org_slug/tool); survive unpublish/republish
-- for automatic injection into /agent/run.
--
-- Subscriptions use qualified names (e.g. "acme/weather") so they survive
-- tool unpublish/republish cycles and are human-readable.

CREATE TABLE IF NOT EXISTS org_marketplace_subscriptions (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    org_id              TEXT NOT NULL REFERENCES orgs(id),
    qualified_tool_name TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    subscribed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(org_id, qualified_tool_name)
);

CREATE INDEX IF NOT EXISTS idx_mp_subs_org
    ON org_marketplace_subscriptions(org_id)
    WHERE is_active = TRUE;

COMMENT ON TABLE org_marketplace_subscriptions IS
    'Per-org subscriptions to marketplace tools. Subscribed tools are injected into /agent/run.';

-- Source migration: 029_marketplace_platform_tools
-- 029: Platform tools in the marketplace catalog.
-- Domain: marketplace
-- Invariant: base_price_usdc is BIGINT atomic USDC; platform tools are owned by the platform, not an org
--
-- Platform-built tools (tools/definitions/) are distinct from org-published
-- tools (org_tools).  They execute in-process, have no webhook URL, and are
-- owned by the platform rather than a specific org.
--
-- This table + seed lets get_marketplace_catalog() UNION platform tools with
-- org tools, giving agents a single catalog view.
--
-- base_price_usdc uses atomic USDC (6 decimals): 1_000_000 = $1.00.

CREATE TABLE IF NOT EXISTS marketplace_platform_tools (
    tool_name       TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    base_price_usdc BIGINT NOT NULL DEFAULT 0
        CHECK (base_price_usdc >= 0 AND base_price_usdc <= 100000000),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mpt_active
    ON marketplace_platform_tools(tool_name)
    WHERE is_active = TRUE;

COMMENT ON TABLE marketplace_platform_tools IS
    'Platform-owned tools published to the marketplace catalog. No org_id owner.';

-- Seed the initial 5 monetised platform tools.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES
    ('get_wallet_portfolio', 'Wallet Portfolio',  4000,  'Multi-chain wallet balances with live token prices in one call'),
    ('web_search',           'Web Search',        10000, 'Real-time web search powered by Tavily'),
    ('get_token_price',      'Token Price',       2000,  'Live crypto prices via CoinGecko with 60s cache, batch up to 50 tokens'),
    ('http_fetch',           'HTTP Fetch',        2000,  'SSRF-protected URL fetch with clean text extraction'),
    ('convert_currency',     'Currency Convert',  2000,  'Fiat and crypto currency conversion in one call')
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 029_sweep_retry_columns
-- 029: Sweep retry columns — add backoff metadata to tool_author_withdrawals
-- Domain: marketplace
-- Invariant: Sweep retries bounded by max_retries; exhausted withdrawals need manual reconciliation
-- so the background sweep worker can track per-org retry state without a
-- separate dead-letter table.
--
-- sweep_attempt_count  : how many sweep cycles have attempted this withdrawal
-- last_sweep_error     : human-readable reason for the last failure
-- next_sweep_at        : NULL = eligible now; future timestamp = in backoff
--
-- Status transitions for sweep-initiated withdrawals:
--   pending  → settled   (CDP transfer succeeded)
--   pending  → failed    (CDP transfer failed, sweep_attempt_count < max)
--   failed   → pending   (backoff elapsed, eligible for retry)
--   failed   → exhausted (sweep_attempt_count >= max_retries)

ALTER TABLE tool_author_withdrawals
    ADD COLUMN IF NOT EXISTS sweep_attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_sweep_error    TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS next_sweep_at       TIMESTAMPTZ;

-- Partial index: only rows eligible for the next sweep cycle need to be scanned.
CREATE INDEX IF NOT EXISTS idx_withdrawals_sweep_eligible
    ON tool_author_withdrawals (next_sweep_at)
    WHERE status IN ('pending', 'failed')
      AND next_sweep_at IS NOT NULL;

-- Status constraint extended to include 'exhausted'.
-- We add it as a new check constraint; existing rows satisfy it because
-- their status is one of the original three values.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tool_author_withdrawals_status_check'
          AND conrelid = 'tool_author_withdrawals'::regclass
    ) THEN
        ALTER TABLE tool_author_withdrawals
            ADD CONSTRAINT tool_author_withdrawals_status_check
            CHECK (status IN ('pending', 'settled', 'failed', 'exhausted'));
    END IF;
END;
$$;

-- Source migration: 030_siwe_nonce_address_binding
-- Migration 030: SIWE nonce address binding
-- Domain: auth
-- Invariant: SIWE nonce bound to the verified wallet address at consumption (defense-in-depth)
-- Defense-in-depth: bind SIWE nonces to the verified wallet address at
-- consumption time.  Nullable for backward compatibility with pre-existing
-- nonces that have no address.

ALTER TABLE siwe_nonces ADD COLUMN IF NOT EXISTS address TEXT;

-- Source migration: 031_activate_bench_tools
-- 031: Activate four bench tools in the marketplace catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC; ON CONFLICT(tool_name) DO NOTHING keeps the seed idempotent
--
-- get_gas_price, resolve_ens, read_contract, and decode_transaction were
-- previously implemented (tools/definitions/) but not exposed as billed
-- marketplace tools.  This migration seeds their catalog rows so that
-- get_marketplace_catalog() includes them and the MCP gateway charges callers.
--
-- Pricing rationale (atomic USDC, 6 decimals; 1_000_000 = $1.00):
--   get_gas_price    2 000  ($0.002) — high-volume, cached 10 s; same tier as token price
--   resolve_ens      3 000  ($0.003) — forward + reverse lookup + avatar; slightly richer than price
--   read_contract    5 000  ($0.005) — general-purpose power tool; arbitrary contract reads
--   decode_transaction 5 000 ($0.005) — tx + receipt fetch + 4byte lookup; high-information output

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES
    (
        'get_gas_price',
        'Gas Price',
        2000,
        'Current EIP-1559 gas fees on Ethereum or Base: base fee, priority fee, '
        'next-block base fee estimate, and network congestion ratio.'
    ),
    (
        'resolve_ens',
        'ENS Resolve',
        3000,
        'Forward lookup (ENS name → address) or reverse lookup (address → primary ENS name). '
        'Returns avatar text record when available.'
    ),
    (
        'read_contract',
        'Read Contract',
        5000,
        'Call any view/pure function on a smart contract with your ABI fragment. '
        'Supports historical queries via block number. State-changing calls are rejected.'
    ),
    (
        'decode_transaction',
        'Decode Transaction',
        5000,
        'Decode transaction calldata into function name and arguments. '
        'Returns status (success/revert), gas used, and block number. '
        'Uses provided ABI or falls back to 4byte.directory.'
    )
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 031_byok_platform_fee
-- Migration 031: BYOK platform fee tracking
-- Domain: billing
-- Invariant: platform_fee_usdc is BIGINT atomic USDC, auditable as a separate line item
-- Adds a platform_fee_usdc column to usage_events so the flat per-run fee
-- charged to BYOK orgs is auditable as a separate line item.

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS platform_fee_usdc BIGINT NOT NULL DEFAULT 0;

-- Source migration: 032_refresh_token_successor
-- Migration 032: refresh token successor column for idempotent rotation
-- Domain: auth
-- Invariant: successor column makes refresh-token rotation idempotent under retries
-- Adds successor_token so that a rotated-but-undelivered refresh token can be
-- replayed within the idempotency window (see refresh_token_idempotency_window_seconds).
-- The column is self-referential: each revoked token points to its replacement.

ALTER TABLE refresh_tokens
    ADD COLUMN IF NOT EXISTS successor_token TEXT REFERENCES refresh_tokens(token) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_successor
    ON refresh_tokens (successor_token)
    WHERE successor_token IS NOT NULL;

-- Source migration: 033_get_token_approvals
-- 033: Add get_token_approvals to the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC ($0.004/call)
--
-- ERC-20 allowance audit tool — returns all non-zero approvals for a wallet
-- across curated DeFi protocol spenders with unlimited-approval risk flags.
-- Priced at $0.004 (4,000 atomic USDC) per call, matching get_wallet_portfolio.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_token_approvals',
    'Token Approvals',
    4000,
    'ERC-20 allowance audit across curated DeFi spenders with unlimited-approval risk flags'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 034_get_defi_positions
-- 034: Add get_defi_positions to the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC ($0.013/call)
--
-- DeFi position aggregator across Aave v3, Compound v3, and Uniswap v3 LP on
-- Ethereum mainnet and Base. Aggregates account-level health (collateral,
-- debt, liquidation risk), per-reserve breakdowns, and LP position data.
-- Priced at $0.013 (13,000 atomic USDC) per call — highest platform tool
-- reflecting multi-protocol RPC cost and differentiated DeFi value.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_defi_positions',
    'DeFi Positions',
    13000,
    'Aggregate DeFi positions across Aave v3, Compound v3, and Uniswap v3 LP on Ethereum and Base'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 035_get_liquidation_risk
-- 035: Add get_liquidation_risk to the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC ($0.010/call)
--
-- Per-wallet DeFi liquidation risk assessment across Aave v3 and Compound v3
-- on Ethereum mainnet and Base. Accepts up to 50 wallet addresses per call
-- and returns tiered risk classification (liquidatable/critical/warning/
-- caution/healthy/no_debt) with an aggregate overall_tier across protocols.
-- Flat-priced at $0.010 (10,000 atomic USDC) per call — priced assuming
-- batched usage (50-wallet alert sweep ≈ $0.0002 per wallet).

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_liquidation_risk',
    'Liquidation Risk',
    10000,
    'Tiered DeFi liquidation risk across Aave v3 and Compound v3 for up to 50 wallets per call'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 036_get_dex_quote
-- 036: Add get_dex_quote to the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC
--
-- On-chain Uniswap v3 swap quote via direct QuoterV2 calls on Ethereum
-- mainnet and Base. Queries all four fee tiers (100/500/3000/10000 bps)
-- in parallel and returns the best amountOut. Pure RPC — no external
-- aggregator dependency. Flat-priced at $0.005 (5,000 atomic USDC) per
-- call, reflecting ~4–6 eth_calls per invocation and free-tier
-- competition from Uniswap frontend / 1inch public API.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_dex_quote',
    'DEX Quote',
    5000,
    'Best Uniswap v3 swap quote across all fee tiers on Ethereum and Base, via direct on-chain QuoterV2 calls'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 037_fix_haiku_pricing
-- Correct Claude Haiku 4.5 pricing seed (migration 022 used Haiku 3 rates).
-- Domain: billing
-- Invariant: Per-model rates in BIGINT atomic USDC; WHERE guard idempotent; does not touch immutable usage_events
-- Anthropic list price: $1.00/M input, $5.00/M output.
-- Teardrop rate (+25% margin): $1.25/M input = 1250 atomic, $6.25/M output = 6250 atomic.
-- Idempotent: WHERE guard prevents double-apply.
-- Does NOT touch usage_events — historical cost_usdc rows are immutable snapshots.

UPDATE pricing_rules
SET
    tokens_in_cost_per_1k  = 1250,
    tokens_out_cost_per_1k = 6250
WHERE id = 'anthropic-haiku-v1'
  AND tokens_in_cost_per_1k = 313;

-- Source migration: 038_org_llm_config_allow_openrouter
-- Relax org_llm_config.provider CHECK constraint to include 'openrouter'.
-- Domain: auth
-- Invariant: Additive provider allow; existing org_llm_config rows unaffected
-- Teardrop uses OpenRouter as an OpenAI-compatible proxy to access models such as
-- DeepSeek V3.2 pinned to US-based inference (DeepInfra) while staying on a
-- single API key. Additive change — existing rows are unaffected.

ALTER TABLE org_llm_config
    DROP CONSTRAINT IF EXISTS org_llm_config_provider_check;

ALTER TABLE org_llm_config
    ADD CONSTRAINT org_llm_config_provider_check
        CHECK (provider IN ('anthropic', 'openai', 'google', 'openrouter'));

-- Source migration: 039_new_model_pricing_seed
-- Migration 039: seed pricing rules for the new shared-pool models (April 2026 pool refresh)
-- Domain: billing
-- Invariant: Rates in BIGINT atomic USDC; ON CONFLICT DO NOTHING keeps the seed idempotent
-- Rates in atomic USDC (1_000_000 = $1.00). All costs include ~25% margin over
-- provider list price.
--
-- DeepSeek V3.2 via OpenRouter / DeepInfra (US, SOC 2):
--   Provider list: ~$0.14/M input, ~$0.28/M output
--   Teardrop rate: $0.175/M input = 175 atomic, $0.35/M output = 350 atomic
--
-- Gemini 3 Flash Preview:
--   Provider list: ~$0.10/M input, ~$0.40/M output
--   Teardrop rate: $0.125/M input = 125 atomic, $0.50/M output = 500 atomic
--
-- Claude Sonnet 4.6:
--   Provider list: $3.00/M input, $15.00/M output
--   Teardrop rate: $3.75/M input = 3750 atomic, $18.75/M output = 18750 atomic
--
-- ON CONFLICT DO NOTHING makes this idempotent on re-run.

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openrouter-deepseek-v3-2-v1',
     'DeepSeek V3.2 (OpenRouter / DeepInfra)',
     'openrouter', 'deepseek/deepseek-v3.2',
     10000, 175, 350, 500, NOW()),

    ('google-gemini-3-flash-preview-v1',
     'Gemini 3 Flash Preview',
     'google', 'gemini-3-flash-preview',
     10000, 125, 500, 500, NOW()),

    ('anthropic-sonnet-4-6-v1',
     'Claude Sonnet 4.6',
     'anthropic', 'claude-sonnet-4-6',
     10000, 3750, 18750, 1000, NOW())

ON CONFLICT (id) DO NOTHING;

-- Source migration: 040_marketplace_catalog_indexes
-- Migration 040: Indexes for marketplace catalog filtering and sorting.
-- Domain: marketplace
-- Invariant: Index-only change; no data mutation
-- Supports the new GET /marketplace/catalog?org_slug=&sort= query parameters.
-- The composite index on (publish_as_mcp, is_active, name) covers the common
-- catalog scan with ORDER BY name.  The price index covers price_asc/price_desc sorts.

CREATE INDEX IF NOT EXISTS idx_org_tools_catalog
    ON org_tools (publish_as_mcp, is_active, name)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_org_tools_catalog_price
    ON org_tools (publish_as_mcp, is_active, base_price_usdc, name)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

-- Source migration: 040_v4_flash_pricing
-- Replace DeepSeek V3.2 pricing with V4 Flash (April 2026 cost-tier upgrade).
-- Domain: billing
-- Invariant: Per-model rates in BIGINT atomic USDC; no usage_events reference the deleted rule (pre-launch)
-- V4 Flash: $0.14/M input, $0.28/M output (provider list) — same Teardrop rates
-- as V3.2 ($0.175/$0.35 per 1M), improving platform margin.
-- No usage_events reference the V3.2 pricing rule (pre-launch), so hard DELETE is safe.

DELETE FROM pricing_rules WHERE id = 'openrouter-deepseek-v3-2-v1';

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openrouter-deepseek-v4-flash-v1',
     'DeepSeek V4 Flash (OpenRouter / US)',
     'openrouter', 'deepseek/deepseek-v4-flash',
     10000, 175, 350, 500, NOW())

ON CONFLICT (id) DO NOTHING;

-- Source migration: 041_byok_tier_pricing
-- Migration 041: BYOK orchestration pricing tier.
-- Domain: billing
-- Invariant: Orchestration tier rate stored in BIGINT atomic USDC
--
-- Adds is_byok column to pricing_rules so the billing engine can resolve a
-- separate rate for BYOK orgs (token-based orchestration fee) vs. standard
-- orgs (model-passthrough cost).
--
-- BYOK rates reflect orchestration overhead only — BYOK users pay their LLM
-- provider directly.  Initial rates: 50 atomic USDC per 1k tokens (~$0.00005/1k).
-- These are intentionally low and can be tuned via DB update without a deploy.
--
-- Resolution order (get_current_pricing_for_model with is_byok=True):
--   exact provider+model BYOK match → provider-level BYOK → global BYOK default

ALTER TABLE pricing_rules ADD COLUMN IF NOT EXISTS is_byok BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_pricing_rules_byok
    ON pricing_rules (is_byok, provider, model, effective_from DESC);

-- Global BYOK default (provider='', model='', is_byok=TRUE)
INSERT INTO pricing_rules
    (id, name, provider, model, is_byok, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('byok-global-v1',
     'BYOK Orchestration (global default)',
     '', '', TRUE,
     0, 50, 50, 0, NOW()),

    ('byok-anthropic-v1',
     'BYOK Orchestration (Anthropic)',
     'anthropic', '', TRUE,
     0, 50, 50, 0, NOW()),

    ('byok-openai-v1',
     'BYOK Orchestration (OpenAI)',
     'openai', '', TRUE,
     0, 50, 50, 0, NOW()),

    ('byok-google-v1',
     'BYOK Orchestration (Google)',
     'google', '', TRUE,
     0, 50, 50, 0, NOW()),

    ('byok-openrouter-v1',
     'BYOK Orchestration (OpenRouter)',
     'openrouter', '', TRUE,
     0, 50, 50, 0, NOW())

ON CONFLICT (id) DO NOTHING;

-- Source migration: 042_org_tools_schema_hash
-- Migration 042: Add schema_hash and last_schema_changed_at to org_tools.
-- Domain: tools
-- Invariant: schema_hash detects tool schema drift for subscribers
--
-- schema_hash is a GENERATED STORED column computed as md5(input_schema::text).
-- JSONB-to-text cast is deterministic (Postgres normalises key ordering in the
-- binary representation), so the hash is consistent across reads and matches
-- the value computed server-side at subscription time.
--
-- last_schema_changed_at records when the input_schema was last modified.
-- Initialised to created_at for existing rows; updated by application code
-- whenever input_schema is mutated.

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS schema_hash TEXT
        GENERATED ALWAYS AS (md5(input_schema::text)) STORED;

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS last_schema_changed_at TIMESTAMPTZ;

-- Back-fill: use created_at as the baseline for all existing rows.
UPDATE org_tools
SET last_schema_changed_at = created_at
WHERE last_schema_changed_at IS NULL;

-- Source migration: 043_marketplace_subscription_schema_hash
-- Migration 043: Add subscribed_schema_hash to org_marketplace_subscriptions.
-- Domain: marketplace
-- Invariant: Tracks subscribed schema version to detect upstream tool schema changes
--
-- Captures the md5(input_schema::text) hash of the tool at the moment a
-- subscriber org subscribes.  The application compares this against the
-- tool's current schema_hash at agent-run time to detect schema drift and
-- log a warning before it causes silent breakage.
--
-- NULL for subscriptions created before this migration (pre-migration rows
-- will accumulate a hash on the next subscribe/re-subscribe call).

ALTER TABLE org_marketplace_subscriptions
    ADD COLUMN IF NOT EXISTS subscribed_schema_hash TEXT;

-- Back-fill active subscriptions: pull the current schema_hash from
-- org_tools via the qualified name (slug/tool_name split).
-- Inactive subscriptions are left NULL — they would need to re-subscribe
-- to get a fresh hash anyway.
UPDATE org_marketplace_subscriptions ms
SET subscribed_schema_hash = (
    SELECT md5(t.input_schema::text)
    FROM org_tools t
    JOIN orgs o ON o.id = t.org_id
    WHERE t.name   = split_part(ms.qualified_tool_name, '/', 2)
      AND o.slug   = split_part(ms.qualified_tool_name, '/', 1)
      AND t.publish_as_mcp = TRUE
      AND t.is_active = TRUE
)
WHERE ms.is_active = TRUE
  AND ms.qualified_tool_name LIKE '%/%';

-- Source migration: 044_fix_gemini3_flash_pricing
-- Migration 044: Correct Gemini 3 Flash Preview pricing.
-- Domain: billing
-- Invariant: Per-model rates in BIGINT atomic USDC; does not touch immutable usage_events
--
-- Migration 039 seeded this model using Gemini 2.0 Flash provider rates
-- (~$0.10/M input, ~$0.40/M output), but Gemini 3 Flash Preview is priced
-- materially higher by Google:
--
--   Google list price (as of 2026-04-28):
--     Input:  $0.50/M tokens  (text / image / video)
--     Output: $3.00/M tokens
--
--   Teardrop rate at ~25% margin:
--     Input:  $0.625/M = 625 atomic USDC per 1k tokens
--     Output: $3.750/M = 3750 atomic USDC per 1k tokens
--
-- The previous row (id = 'google-gemini-3-flash-preview-v1') had:
--     tokens_in_cost_per_1k  = 125   ($0.125/M — 75% below provider cost)
--     tokens_out_cost_per_1k = 500   ($0.500/M — 83% below provider cost)
--
-- UPDATE is used (not INSERT … ON CONFLICT DO NOTHING) so that the corrected
-- rates take effect immediately without requiring a row delete + re-seed.

UPDATE pricing_rules
SET
    name                   = 'Gemini 3 Flash Preview (corrected 2026-04-28)',
    tokens_in_cost_per_1k  = 625,
    tokens_out_cost_per_1k = 3750,
    effective_from         = NOW()
WHERE id = 'google-gemini-3-flash-preview-v1';

-- Source migration: 045_get_token_price_historical
-- 045: Add get_token_price_historical to the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC
--
-- Historical crypto price tool wrapping CoinGecko /coins/{id}/market_chart.
-- Returns period statistics (start, end, % change, high, low) plus a
-- downsampled daily price series for windows of 1–365 days. Eliminates the
-- web_search loop that previously occurred on every time-comparative query.
-- Priced at $0.004 (4,000 atomic USDC) per call — one upstream API request
-- per token, matching get_wallet_portfolio's per-call cost profile.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_token_price_historical',
    'Token Price History',
    4000,
    'Historical crypto price data via CoinGecko — period stats and daily series for 1–365 day windows'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 046_web3_tools_marketplace_seed
-- 046: Seed four web3 primitives into the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC; ON CONFLICT(tool_name) DO NOTHING keeps the seed idempotent
--
-- These tools are fully implemented in the agent registry (tools/definitions/)
-- and have been in production use since the baseline migration, but were never
-- seeded into marketplace_platform_tools.  Without a row here, billing falls
-- through to default_cost=0, meaning all calls are effectively free.
--
-- Scope: get_eth_balance, get_erc20_balance, get_block, get_transaction.
--
-- Excluded intentionally:
--   calculate      — pure in-process math; zero marginal cost.
--   delegate_to_agent — has a dedicated a2a_delegation billing path;
--                       seeding would cause double-billing.
--   get_datetime   — in-process; zero marginal cost.
--   count_text_stats — in-process text statistics; zero marginal cost.
--
-- Pricing rationale (atomic USDC; 1_000_000 = $1.00):
--   get_eth_balance  : 1,000 ($0.001) — 1 RPC call (eth_getBalance).
--                      Below get_gas_price ($0.002); lowest utility of the set.
--   get_erc20_balance: 2,000 ($0.002) — 3 concurrent contract calls
--                      (balanceOf + symbol + decimals).  Richer output
--                      justifies parity with get_gas_price ($0.002).
--   get_block        : 1,000 ($0.001) — 1 RPC call (eth_getBlockByNumber/Hash).
--                      Same complexity as get_eth_balance.
--   get_transaction  : 2,000 ($0.002) — 2 RPC calls (eth_getTransaction +
--                      eth_getTransactionReceipt).  Two round-trips warrant
--                      the same price as get_erc20_balance.

-- get_eth_balance: native ETH balance lookup on Ethereum or Base.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_eth_balance',
    'ETH Balance',
    1000,
    'Get the native ETH balance of an Ethereum or Base address'
)
ON CONFLICT (tool_name) DO NOTHING;

-- get_erc20_balance: ERC-20 token balance with symbol and decimal normalisation.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_erc20_balance',
    'ERC-20 Balance',
    2000,
    'Get the ERC-20 token balance of a wallet, including symbol and decimals'
)
ON CONFLICT (tool_name) DO NOTHING;

-- get_block: block header and summary for Ethereum or Base.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_block',
    'Block Details',
    1000,
    'Get details for an Ethereum or Base block by number, hash, or ''latest'''
)
ON CONFLICT (tool_name) DO NOTHING;

-- get_transaction: full transaction details and receipt for Ethereum or Base.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_transaction',
    'Transaction',
    2000,
    'Get details and receipt for an Ethereum or Base transaction by hash'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 047_get_protocol_tvl
-- 047: Add get_protocol_tvl to the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC
--
-- DeFiLlama-backed TVL lookup for DeFi protocols. Returns current TVL in USD,
-- 7-day and 30-day percentage change, per-chain breakdown, and an optional
-- daily historical series. Uses the DeFiLlama free public API (no key, no auth).
-- Priced at $0.003 (3,000 atomic USDC) — 1-2 upstream HTTP calls, free source,
-- matching resolve_ens ($0.003) in the per-call cost tier.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_protocol_tvl',
    'Protocol TVL',
    3000,
    'DeFi protocol TVL from DeFiLlama — current value, 7d/30d trend, chain breakdown, optional daily history'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 048_get_yield_rates
-- 048: Add get_yield_rates to the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC
--
-- DeFiLlama /pools aggregator — returns yield pools filtered by protocol,
-- chain, minimum TVL, and minimum APY, sorted by APY descending. Covers
-- Aave, Compound, Curve, Yearn, and 1,000+ other protocols across all chains.
-- Single upstream HTTP call; filtering is client-side from the full pool list.
-- Priced at $0.004 (4,000 atomic USDC) — larger response payload and
-- filtering compute, matching get_token_price_historical in the cost tier.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_yield_rates',
    'Yield Rates',
    4000,
    'DeFi yield pool rates from DeFiLlama — APY, TVL, and token breakdown across 1,000+ protocols and chains'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 049_org_tools_output_schema
-- Migration 049: persisted output schema contract for webhook tools
-- Domain: tools
-- Invariant: Output schema is org-scoped per webhook tool; additive columns only
ALTER TABLE org_tools
ADD COLUMN IF NOT EXISTS output_schema JSONB;

-- Keep drift-detection support parallel to existing input schema hash.
ALTER TABLE org_tools
ADD COLUMN IF NOT EXISTS output_schema_hash TEXT
GENERATED ALWAYS AS (
    CASE
        WHEN output_schema IS NOT NULL THEN md5(output_schema::text)
        ELSE NULL
    END
) STORED;

-- Source migration: 050_usage_events_billable_accounting
-- Migration 050: separate attempt metrics from billable/failure metrics in usage accounting
-- Domain: billing
-- Invariant: Distinguishes billable from attempt/failure metrics; monetary amounts remain BIGINT atomic USDC
ALTER TABLE usage_events
ADD COLUMN IF NOT EXISTS billable_tool_calls INTEGER NOT NULL DEFAULT 0;

ALTER TABLE usage_events
ADD COLUMN IF NOT EXISTS billable_tool_names TEXT NOT NULL DEFAULT '[]';

ALTER TABLE usage_events
ADD COLUMN IF NOT EXISTS failed_tool_calls INTEGER NOT NULL DEFAULT 0;

ALTER TABLE usage_events
ADD COLUMN IF NOT EXISTS failed_tool_names TEXT NOT NULL DEFAULT '[]';

-- Source migration: 051_gpt54_mini_pricing_seed
-- Seed GPT-5.4 mini pricing for synthesis-turn cost attribution.
-- Domain: billing
-- Invariant: Per-1k-token rates in BIGINT atomic USDC
-- OpenAI list price: $0.75/M input, $4.50/M output.
-- Teardrop rate (+25% margin): $0.9375/M input, $5.625/M output.
-- Stored in atomic USDC per 1k tokens:
--   input  = 938   (rounded from 937.5)
--   output = 5625

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openai-gpt54-mini-v1',
     'GPT-5.4 mini',
     'openai', 'gpt-5.4-mini',
     10000, 938, 5625, 1000, NOW())

ON CONFLICT (id) DO NOTHING;

-- Source migration: 052_get_lending_rates
-- 052: Add get_lending_rates to the marketplace platform tools catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC
--
-- On-chain lending-rate snapshot for Aave v3 and Compound v3 across Ethereum
-- and Base. The tool fans out via Multicall3 and returns per-asset supply and
-- borrow APY plus Compound utilization.
--
-- Pricing rationale:
--   get_lending_rates => $0.003 (3,000 atomic USDC)
--   - More RPC-intensive than get_protocol_tvl/get_yield_rates HTTP lookups,
--     but still deterministic read-only calls with bounded payload.
--   - Keeps parity with get_protocol_tvl ($0.003) while remaining below the
--     broad market scan tier represented by get_yield_rates ($0.004).

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_lending_rates',
    'Lending Rates',
    3000,
    'On-chain lending APY snapshot for Aave v3 and Compound v3 by asset and chain'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 053_zero_cost_inprocess_tool_overrides
-- Migration 053: enforce zero-cost pricing for in-process utility tools
-- Domain: billing
-- Invariant: In-process utility tools priced at 0 atomic USDC
--
-- These tools do not rely on external paid APIs and should never consume the
-- default per-tool billing rate from pricing_rules.tool_call_cost.

INSERT INTO tool_pricing_overrides (tool_name, cost_usdc, description)
VALUES
    ('calculate', 0, 'Pure in-process arithmetic utility; zero marginal cost'),
    ('get_datetime', 0, 'In-process datetime utility; zero marginal cost'),
    ('count_text_stats', 0, 'In-process text statistics utility; zero marginal cost')
ON CONFLICT (tool_name) DO UPDATE
SET
    cost_usdc = EXCLUDED.cost_usdc,
    description = EXCLUDED.description,
    updated_at = NOW();

-- Source migration: 054_usage_events_cache_tokens
-- Migration 054: provider cache-token telemetry columns for usage benchmarking
-- Domain: billing
-- Invariant: Adds cache_read/creation token columns for accurate per-token billing; additive only

ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cache_creation_tokens INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_usage_org_created_cache
    ON usage_events (org_id, created_at DESC)
    WHERE (cache_read_tokens > 0 OR cache_creation_tokens > 0);

-- Source migration: 055_get_only_webhook_tools
-- Migration 055: Enforce GET-only active org webhook tools.
-- Domain: tools
-- Invariant: Active webhook tools restricted to GET (SSRF/safety hardening)

-- Existing active POST/PUT tools are deactivated and must be re-registered
-- with GET-compatible webhook endpoints.
UPDATE org_tools
SET is_active = FALSE,
    updated_at = NOW()
WHERE is_active = TRUE
  AND webhook_method <> 'GET';

-- Active tools must always be GET-only.
ALTER TABLE org_tools
ADD CONSTRAINT chk_active_tool_get_only
CHECK (NOT is_active OR webhook_method = 'GET');

-- Source migration: 056_align_web_search_marketplace_price
-- 056: Align marketplace web_search price with agent-run override pricing.
-- Domain: marketplace
-- Invariant: Marketplace price aligned to the agent-run override; BIGINT atomic USDC
--
-- Background:
-- - tool_pricing_overrides seeds web_search at 15,000 atomic USDC ($0.015)
-- - marketplace_platform_tools currently has web_search at 10,000 ($0.010)
-- This migration aligns marketplace catalog pricing to 15,000 so direct MCP
-- gateway calls and agent-run billing follow the same per-call price.
--
-- Product note: this increases direct marketplace web_search price by $0.005.

UPDATE marketplace_platform_tools
SET base_price_usdc = 15000
WHERE tool_name = 'web_search';

-- Source migration: 057_credit_ledger_debit_index
-- Migration 057: narrow index for 24h debit-spend aggregates used by billing limits
-- Domain: billing
-- Invariant: Index supporting 24h rolling spend computation; no data mutation
CREATE INDEX IF NOT EXISTS idx_credit_ledger_debit_time
    ON org_credit_ledger (org_id, created_at DESC)
    WHERE operation = 'debit';

-- Source migration: 058_marketplace_dashboard_catalog
-- 058: Marketplace dashboard catalog support.
-- Domain: marketplace
-- Invariant: Supports dashboard catalog views; no monetary change
--
-- Adds public-safe catalog metadata for O4 dashboard/SEO pages:
--   - category filters for community and platform marketplace tools
--   - aggregate call stats decoupled from financial author earnings
--
-- Financial ledgers remain unchanged.  All money fields continue to use
-- atomic USDC integers; this migration only adds non-financial metadata.

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT ''
    CHECK (category IN ('', 'defi', 'search', 'data', 'communication', 'utility'));

ALTER TABLE marketplace_platform_tools
    ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT ''
    CHECK (category IN ('', 'defi', 'search', 'data', 'communication', 'utility'));

CREATE TABLE IF NOT EXISTS marketplace_tool_call_stats (
    qualified_tool_name TEXT PRIMARY KEY,
    tool_type           TEXT NOT NULL CHECK (tool_type IN ('platform', 'community')),
    author_org_id       TEXT REFERENCES orgs(id),
    total_calls         BIGINT NOT NULL DEFAULT 0 CHECK (total_calls >= 0),
    first_call_at       TIMESTAMPTZ,
    last_call_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_marketplace_tool_call_stats_popularity
    ON marketplace_tool_call_stats (total_calls DESC, qualified_tool_name ASC);

CREATE INDEX IF NOT EXISTS idx_marketplace_tool_call_stats_author
    ON marketplace_tool_call_stats (author_org_id, total_calls DESC)
    WHERE author_org_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_org_tools_catalog_category
    ON org_tools (category, publish_as_mcp, is_active, name)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_marketplace_platform_tools_category
    ON marketplace_platform_tools (category, tool_name)
    WHERE is_active = TRUE;

-- Best-effort category seeds for platform tools. Community tool category is
-- author-controlled through the org tool API and defaults to uncategorized.
UPDATE marketplace_platform_tools
SET category = 'search'
WHERE tool_name IN ('web_search', 'http_fetch') AND category = '';

UPDATE marketplace_platform_tools
SET category = 'defi'
WHERE tool_name IN (
    'get_wallet_portfolio',
    'get_token_price',
    'get_token_price_historical',
    'get_protocol_tvl',
    'get_yield_rates',
    'get_lending_rates',
    'get_dex_quote',
    'get_defi_positions',
    'get_liquidation_risk',
    'get_token_approvals'
) AND category = '';

UPDATE marketplace_platform_tools
SET category = 'data'
WHERE tool_name IN (
    'get_eth_balance',
    'get_erc20_balance',
    'get_block',
    'get_transaction',
    'convert_currency'
) AND category = '';

COMMENT ON TABLE marketplace_tool_call_stats IS
    'Public-safe aggregate marketplace call counts for dashboard/SEO catalog pages. Not a financial ledger.';

-- Source migration: 059_x402_payment_nonces
-- Migration 059: x402 payment-header replay guard (concurrent-replay protection)
-- Domain: billing
-- Invariant: PRIMARY KEY on nonce_hash makes the first INSERT win; concurrent
-- replays of the same signed payment header are rejected before the tool runs.
--
-- The blockchain already prevents an EIP-3009 authorization from settling
-- twice on-chain. This table additionally closes the *concurrent* window where
-- two in-flight requests carrying the identical payment header both pass
-- verification and both execute the paid tool before either settles.
--
-- nonce_hash is the SHA-256 of the raw base64 payment header. claimed_at drives
-- the 24h retention sweep (a fresh authorization is required after that anyway).

CREATE TABLE IF NOT EXISTS x402_payment_nonces (
    nonce_hash  TEXT PRIMARY KEY,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_x402_payment_nonces_claimed_at
    ON x402_payment_nonces (claimed_at);

-- Source migration: 060_org_tools_partial_unique_name
-- Migration 060: Replace unconditional UNIQUE(org_id, name) with a partial
-- unique index so that soft-deleted (is_active=FALSE) tools do not block
-- creation of a new active tool with the same name.
-- Domain: tools
-- Invariant: Only one active tool per org may bear a given name; deleted/paused
-- tools release their name for immediate reuse.

-- Safety check: there must be no active duplicates before we swap the constraint.
DO $$
DECLARE
    dup_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO dup_count
    FROM (
        SELECT org_id, name
        FROM org_tools
        WHERE is_active = TRUE
        GROUP BY org_id, name
        HAVING COUNT(*) > 1
    ) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'Cannot drop UNIQUE constraint: % active duplicate(s) exist', dup_count;
    END IF;
END $$;

-- Drop the table-level UNIQUE constraint (auto-named org_tools_org_id_name_key).
ALTER TABLE org_tools DROP CONSTRAINT IF EXISTS org_tools_org_id_name_key;

-- Add a partial unique index: two active tools in the same org cannot share a name.
-- Deleted/paused tools (is_active=FALSE) are ignored by this index, so their names
-- are immediately freed for reuse.
CREATE UNIQUE INDEX IF NOT EXISTS org_tools_org_id_name_active_uq
    ON org_tools (org_id, name)
    WHERE is_active = TRUE;

COMMENT ON INDEX org_tools_org_id_name_active_uq IS
    'Enforces unique tool names per org for active tools only; soft-deleted names are reusable.';

-- Source migration: 061_marketplace_catalog_search
-- Migration 061: Trigram indexes for marketplace catalog free-text search.
-- Domain: marketplace
-- Invariant: Additive index-only change; existing catalog sort and cursor semantics remain unchanged.
-- Supports GET /marketplace/catalog?q= partial matching across tool and author metadata.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_org_tools_name_trgm
    ON org_tools USING gin (name gin_trgm_ops)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_org_tools_description_trgm
    ON org_tools USING gin (description gin_trgm_ops)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_org_tools_marketplace_desc_trgm
    ON org_tools USING gin (marketplace_description gin_trgm_ops)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_orgs_name_trgm
    ON orgs USING gin (name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_orgs_slug_trgm
    ON orgs USING gin (slug gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_platform_tools_tool_name_trgm
    ON marketplace_platform_tools USING gin (tool_name gin_trgm_ops)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_platform_tools_display_name_trgm
    ON marketplace_platform_tools USING gin (display_name gin_trgm_ops)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_platform_tools_description_trgm
    ON marketplace_platform_tools USING gin (description gin_trgm_ops)
    WHERE is_active = TRUE;

-- Source migration: 062_a2a_inbound_events
-- Migration 062: inbound A2A audit ledger
-- Domain: A2A
-- Invariant: Inbound A2A requests emit immutable audit rows across accepted and rejected billing outcomes
--
-- Adds a dedicated inbound A2A audit table so operators can inspect caller
-- identity, payment failures, and run outcomes without reconstructing history
-- from usage_events and settlement side effects alone.

CREATE TABLE IF NOT EXISTS a2a_inbound_events (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL DEFAULT '',
    usage_event_id  TEXT,
    caller_org_id   TEXT NOT NULL DEFAULT '',
    caller_user_id  TEXT NOT NULL DEFAULT '',
    caller_address  TEXT NOT NULL DEFAULT '',
    caller_ip       TEXT NOT NULL DEFAULT '',
    auth_method     TEXT NOT NULL DEFAULT '',
    context_id      TEXT NOT NULL DEFAULT '',
    task_id         TEXT NOT NULL DEFAULT '',
    task_state      TEXT NOT NULL
                  CHECK (task_state IN ('completed', 'failed', 'timeout', 'rejected_payment', 'rejected_auth_credit')),
    cost_usdc       BIGINT NOT NULL DEFAULT 0,
    settlement_tx   TEXT NOT NULL DEFAULT '',
    billing_method  TEXT NOT NULL DEFAULT '',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_events_org
    ON a2a_inbound_events (caller_org_id, created_at DESC)
    WHERE caller_org_id != '';

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_events_address
    ON a2a_inbound_events (caller_address, created_at DESC)
    WHERE caller_address != '';

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_events_run
    ON a2a_inbound_events (run_id);

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_events_state
    ON a2a_inbound_events (task_state, created_at DESC);

-- Source migration: 063_org_tools_mcp_backed
-- Migration 063: Allow org_tools rows to target MCP server tools.
-- Domain: tools / marketplace / MCP
-- Invariant: a tool row must target either a webhook URL or an MCP server tool.

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS mcp_server_id TEXT
        REFERENCES org_mcp_servers(id) ON DELETE RESTRICT;

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS mcp_tool_name TEXT;

ALTER TABLE org_tools
    ALTER COLUMN webhook_url DROP NOT NULL;

ALTER TABLE org_tools
    DROP CONSTRAINT IF EXISTS org_tools_exec_target_check;

ALTER TABLE org_tools
    ADD CONSTRAINT org_tools_exec_target_check
    CHECK (webhook_url IS NOT NULL OR mcp_server_id IS NOT NULL);

ALTER TABLE org_tools
    DROP CONSTRAINT IF EXISTS org_tools_mcp_target_pair_check;

ALTER TABLE org_tools
    ADD CONSTRAINT org_tools_mcp_target_pair_check
    CHECK (
        (mcp_server_id IS NULL AND mcp_tool_name IS NULL)
        OR (mcp_server_id IS NOT NULL AND mcp_tool_name IS NOT NULL)
    );

-- Source migration: 064_scheduled_runs
-- Migration 064: scheduled runs
-- Domain: scheduling
-- Invariant: scheduled_runs drives cron/interval agent execution; claims use SKIP LOCKED for multi-worker safety
-- Tables: scheduled_runs

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

-- Source migration: 065_event_triggers
-- 065_event_triggers.sql
-- Adds reactive (event-triggered) runs on top of the scheduled_runs subsystem.
-- Additive + backward-compatible: loosens NOT NULL on interval-only columns so a
-- single table can hold both interval schedules and event triggers. The polling
-- worker already filters schedule_kind = 'interval', so event rows are inert to it.
--
-- Invariant: per-trigger signing secrets are stored ONLY as SHA-256 hashes; the
-- plaintext secret is returned once at creation/rotation (mirrors migration 005).

-- 1. Allow the 'event' schedule kind.
ALTER TABLE scheduled_runs DROP CONSTRAINT IF EXISTS scheduled_runs_kind_chk;
ALTER TABLE scheduled_runs
    ADD CONSTRAINT scheduled_runs_kind_chk CHECK (schedule_kind IN ('interval', 'event'));

-- 2. Interval-only columns become optional for event rows.
ALTER TABLE scheduled_runs ALTER COLUMN interval_seconds DROP NOT NULL;
ALTER TABLE scheduled_runs DROP CONSTRAINT IF EXISTS scheduled_runs_interval_chk;
ALTER TABLE scheduled_runs
    ADD CONSTRAINT scheduled_runs_interval_chk CHECK (interval_seconds IS NULL OR interval_seconds > 0);
ALTER TABLE scheduled_runs ALTER COLUMN next_run_at DROP NOT NULL;

-- 3. Event-trigger routing + auth columns.
ALTER TABLE scheduled_runs ADD COLUMN IF NOT EXISTS trigger_token TEXT;
ALTER TABLE scheduled_runs ADD COLUMN IF NOT EXISTS secret_hash TEXT;

-- Public, non-secret routing id used in the inbound URL. Unique among event rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_runs_trigger_token
    ON scheduled_runs (trigger_token)
    WHERE trigger_token IS NOT NULL;

-- An event row must carry both a routing token and a secret hash.
ALTER TABLE scheduled_runs DROP CONSTRAINT IF EXISTS scheduled_runs_event_shape_chk;
ALTER TABLE scheduled_runs
    ADD CONSTRAINT scheduled_runs_event_shape_chk CHECK (
        schedule_kind <> 'event'
        OR (trigger_token IS NOT NULL AND secret_hash IS NOT NULL)
    );

-- 4. Idempotency / at-most-once reservation for inbound dispatches.
-- Append-only; insert-first reservation guarantees a given (trigger, key) runs once.
CREATE TABLE IF NOT EXISTS event_dispatch_keys (
    schedule_id TEXT NOT NULL REFERENCES scheduled_runs(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (schedule_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_event_dispatch_keys_created_at
    ON event_dispatch_keys (created_at DESC);

-- Source migration: 066_tool_call_events
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

-- Source migration: 067_run_decisions
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

-- Source migration: 068_org_tool_exclusions
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

-- Source migration: 069_onboarding_credit_grants
-- Migration 069: verified-email onboarding credit grants and retry outbox
-- Domain: billing / onboarding
-- Invariant: one immutable grant marker per organisation; balance and ledger
-- mutation is performed in the same transaction as the marker insert.

CREATE TABLE IF NOT EXISTS org_onboarding_credit_grants (
    org_id          TEXT        PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    amount_usdc     BIGINT      NOT NULL CHECK (amount_usdc > 0),
    ledger_entry_id TEXT        NOT NULL UNIQUE REFERENCES org_credit_ledger(id) DEFERRABLE INITIALLY DEFERRED,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS org_onboarding_credit_outbox (
    org_id          TEXT        PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    amount_usdc     BIGINT      NOT NULL CHECK (amount_usdc > 0),
    attempts        INTEGER     NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Source migration: 070_a2a_delegation_task_type
-- 070: Bounded task-type telemetry for outbound A2A delegation events.
-- Domain: A2A / non-financial analytics
-- Invariant: This additive field must not affect credit debits, x402 settlement,
-- or the append-only financial audit trail.

ALTER TABLE a2a_delegation_events
    ADD COLUMN IF NOT EXISTS task_type TEXT NOT NULL DEFAULT 'general'
    CHECK (task_type IN ('general', 'research', 'analysis', 'data_retrieval', 'coding', 'transaction', 'automation'));

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_events_org_task_type_created
    ON a2a_delegation_events (org_id, task_type, created_at DESC);

COMMENT ON COLUMN a2a_delegation_events.task_type IS
    'Bounded delegation task class. Never contains raw task descriptions or credentials.';

-- Source migration: 071_mcp_client_schema_hash
-- 071: Track discovered external MCP tool-inventory schema changes.
-- Domain: MCP client / non-financial telemetry
-- Invariant: Hashes describe public tool metadata only and must never block
-- discovery, execution, billing, or settlement paths.

ALTER TABLE org_mcp_servers
    ADD COLUMN IF NOT EXISTS schema_hash TEXT;

ALTER TABLE org_mcp_servers
    ADD COLUMN IF NOT EXISTS last_schema_changed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_org_mcp_servers_org_schema_changed
    ON org_mcp_servers (org_id, last_schema_changed_at DESC)
    WHERE last_schema_changed_at IS NOT NULL;

COMMENT ON COLUMN org_mcp_servers.schema_hash IS
    'SHA-256 of the last successfully discovered MCP tool inventory.';

COMMENT ON COLUMN org_mcp_servers.last_schema_changed_at IS
    'Timestamp of the last successfully discovered MCP tool-inventory change.';

-- Source migration: 072_reserve_platform_org_slug
-- 072: Reserve the platform namespace for first-party marketplace tools.
-- Domain: marketplace / tenancy
-- Invariant: Community organisations cannot claim the platform/{tool} namespace.

-- NOT VALID preserves deployability if a legacy row already owns the slug while
-- enforcing the reservation for every new or updated organisation. Catalog
-- queries exclude such legacy rows until they are remediated.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_orgs_slug_reserved_platform'
          AND conrelid = 'orgs'::regclass
    ) THEN
        ALTER TABLE orgs
            ADD CONSTRAINT chk_orgs_slug_reserved_platform
            CHECK (slug <> 'platform') NOT VALID;
    END IF;
END;
$$;

-- Source migration: 073_data_foundations
-- Migration 073: data strategy foundations and disposable-data retention support
-- Domain: telemetry / onboarding analytics / operational retention
-- Invariants:
--   * Financial and A2A audit ledgers remain immutable and are never retention targets.
--   * Existing API and SDK contracts remain additive and backward compatible.
--   * Checkpoint cleanup is coordinated through per-thread activity rows.

-- ── Acquisition attribution ─────────────────────────────────────────────────
-- First-touch attribution only. The API accepts a normalized source token; the
-- database constraint protects administrative and future non-HTTP writers too.
ALTER TABLE orgs
    ADD COLUMN IF NOT EXISTS acquisition_source TEXT NOT NULL DEFAULT '';

ALTER TABLE orgs
    ADD CONSTRAINT orgs_acquisition_source_format_chk
    CHECK (acquisition_source ~ '^[a-z0-9_-]{0,64}$');

-- ── Canonical run provenance ─────────────────────────────────────────────────
-- usage_events is the one-record-per-run operational dimension. Keep legacy
-- blank run IDs outside the unique constraint for backwards compatibility.
ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'api';

ALTER TABLE usage_events
    ADD CONSTRAINT usage_events_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_events_run_id
    ON usage_events (run_id)
    WHERE run_id <> '';

-- ── ML telemetry compatibility ───────────────────────────────────────────────
ALTER TABLE tool_call_events
    ADD COLUMN IF NOT EXISTS schema_version SMALLINT NOT NULL DEFAULT 1;

ALTER TABLE run_decisions
    ADD COLUMN IF NOT EXISTS schema_version SMALLINT NOT NULL DEFAULT 1;

ALTER TABLE run_decisions
    ADD COLUMN IF NOT EXISTS taxonomy_version SMALLINT NOT NULL DEFAULT 1;

-- ── Race-safe LangGraph checkpoint retention ─────────────────────────────────
-- The currently installed LangGraph saver owns checkpoint_blobs. Older Teardrop
-- baseline schemas predate that table, so create it here before retention uses it.
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL,
    version       TEXT NOT NULL,
    type          TEXT NOT NULL,
    blob          BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_thread_activity (
    thread_id        TEXT PRIMARY KEY,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Existing threads begin a fresh TTL window. This avoids deriving a timestamp
-- from legacy checkpoint JSON and prevents a migration-time active-run race.
INSERT INTO checkpoint_thread_activity (thread_id, last_activity_at)
SELECT DISTINCT thread_id, NOW()
FROM checkpoints
ON CONFLICT (thread_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_checkpoint_thread_activity_last_activity
    ON checkpoint_thread_activity (last_activity_at);

-- ── Disposable operational records ───────────────────────────────────────────
-- These indexes support bounded retention sweeps. org_tool_events retains
-- create/update/delete audit rows permanently; only execution noise is swept.
CREATE INDEX IF NOT EXISTS idx_scheduled_run_results_created_at
    ON scheduled_run_results (created_at);

CREATE INDEX IF NOT EXISTS idx_org_tool_events_disposable_created_at
    ON org_tool_events (created_at)
    WHERE event_type IN ('executed', 'failed');

-- Source migration: 074_usage_events_runner_version
-- Migration 074: deployment provenance for canonical agent-run records
-- Domain: telemetry / deployment provenance
-- Invariant: Additive column; historical rows keep the empty default; never referenced by billing or settlement
-- Historical rows remain explicitly unknown (empty value); new events are
-- stamped by teardrop.usage from the dependency-free APP_VERSION constant.

ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS runner_version TEXT NOT NULL DEFAULT '';

-- Source migration: 075_marketplace_reputation_v2
-- Migration 075: recency-aware marketplace reputation diagnostics
-- Domain: marketplace / reputation
-- Invariant: Derived telemetry only; never affects settlement, author earnings, tool pricing, or financial ledgers
-- Derived telemetry only: this has no effect on settlement, author earnings,
-- tool pricing, or the immutable financial ledgers.

ALTER TABLE marketplace_tool_call_stats
    ADD COLUMN IF NOT EXISTS reputation_sample_size NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reputation_confidence NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reputation_freshness NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reputation_task_success JSONB NOT NULL DEFAULT '{}'::JSONB;

-- Source migration: 076_telemetry_run_starts
-- Migration 076: immutable run-start index for telemetry completeness metrics
-- This is a non-financial observability denominator. It records no prompts,
-- tool arguments, model output, credentials, or payment information.

CREATE TABLE IF NOT EXISTS telemetry_run_starts (
    run_id     TEXT        PRIMARY KEY,
    org_id     TEXT        NOT NULL DEFAULT '',
    source     TEXT        NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT telemetry_run_starts_source_chk
        CHECK (source IN ('api', 'schedule', 'trigger', 'a2a'))
);

CREATE INDEX IF NOT EXISTS idx_telemetry_run_starts_source_started
    ON telemetry_run_starts (source, started_at DESC);

-- Source migration: 077_telemetry_run_starts_retention
-- Migration 077: retention index for telemetry completeness denominators
-- Domain: telemetry / retention
-- Invariant: Index-only, non-financial; supports ordered batched cleanup of disposable telemetry
-- telemetry_run_starts is non-financial and retained only for the configured
-- completeness-reporting window. This index supports ordered, batched cleanup.

CREATE INDEX IF NOT EXISTS idx_telemetry_run_starts_started_at
    ON telemetry_run_starts (started_at);

-- Source migration: 078_run_decisions_thread_message
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

-- Source migration: 079_gemini_3_6_flash_pricing
-- Seed pricing for Gemini 3.6 Flash.
-- Domain: billing
-- Invariant: Per-1k-token rates in BIGINT atomic USDC
-- Provider list price: $1.5000000/M input, $7.5000000/M output.
-- Teardrop rate (+25% margin): 1875 input, 9375 output atomic USDC per 1k tokens.

DELETE FROM pricing_rules WHERE id = 'google-gemini-3-flash-preview-v1';

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('google-gemini-3-6-flash-v1',
     'Gemini 3.6 Flash',
     'google', 'gemini-3.6-flash',
     10000, 1875, 9375, 1000, NOW())

ON CONFLICT (id) DO NOTHING;

-- Source migration: 080_claude_sonnet_5_pricing
-- Seed pricing for Claude Sonnet 5.
-- Domain: billing
-- Invariant: Per-1k-token rates in BIGINT atomic USDC
-- Provider list price: $2.000000/M input, $10.00000/M output.
-- Teardrop rate (+25% margin): 2500 input, 12500 output atomic USDC per 1k tokens.

DELETE FROM pricing_rules WHERE id = 'anthropic-sonnet-4-6-v1';

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('anthropic-claude-sonnet-5-v1',
     'Claude Sonnet 5',
     'anthropic', 'claude-sonnet-5',
     10000, 2500, 12500, 1000, NOW())

ON CONFLICT (id) DO NOTHING;

-- Source migration: 081_reputation_public_metrics
-- Migration 081: public marketplace reputation metrics
-- Domain: marketplace / reputation
-- Invariant: Derived telemetry only; CHECK constraints bound public metrics; financial ledgers unchanged
-- Derived telemetry only; financial ledgers and settlement paths are unchanged.

ALTER TABLE marketplace_tool_call_stats
    ADD COLUMN IF NOT EXISTS success_rate NUMERIC NOT NULL DEFAULT 0
        CHECK (success_rate >= 0 AND success_rate <= 1),
    ADD COLUMN IF NOT EXISTS average_latency_ms NUMERIC NOT NULL DEFAULT 0
        CHECK (average_latency_ms >= 0),
    ADD COLUMN IF NOT EXISTS unique_caller_count BIGINT NOT NULL DEFAULT 0
        CHECK (unique_caller_count >= 0);

CREATE INDEX IF NOT EXISTS idx_tool_call_events_tool_org
    ON tool_call_events (tool_name, org_id);

-- Source migration: 082_deepseek_v4_flash_0731_openrouter_us_pricing
-- Seed pricing for DeepSeek V4 Flash 0731 (OpenRouter / US).
-- Domain: billing
-- Invariant: Per-1k-token rates in BIGINT atomic USDC
-- Provider list price: $0.09000000/M input, $0.18000000/M output.
-- Teardrop rate (+25% margin): 113 input, 225 output atomic USDC per 1k tokens.
-- The retired model is deprecated and no longer selected by production routing.
-- usage_events store immutable cost snapshots, so this does not alter billing history.

DELETE FROM pricing_rules WHERE id = 'openrouter-deepseek-v4-flash-v1';

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openrouter-deepseek-v4-flash-0731-v1',
     'DeepSeek V4 Flash 0731 (OpenRouter / US)',
     'openrouter', 'deepseek/deepseek-v4-flash-0731',
     10000, 113, 225, 500, NOW())

ON CONFLICT (id) DO NOTHING;

-- Source migration: 083_defi_landscape_tools
-- 083: Add chain-health and DEX-volume analysis tools to the platform catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC
--
-- Both tools use bounded DeFiLlama aggregate requests and are priced at
-- $0.003 (3,000 atomic USDC), matching get_protocol_tvl.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES
    (
        'get_chain_metrics',
        'Chain Metrics',
        3000,
        'Blockchain ecosystem health from DeFiLlama — current TVL, 7d/30d TVL trend, and aggregate fees'
    ),
    (
        'get_dex_volume',
        'DEX Volume',
        3000,
        'DEX landscape activity from DeFiLlama — 24h/7d/30d volume, changes, and global volume share'
    )
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 084_telemetry_source_propagation
-- Migration 084: propagate run source into ML telemetry rows.
-- Domain: agent telemetry / ML data foundation
-- Invariant: Additive only; never referenced by billing or settlement paths.
-- Legacy rows without a matching usage event retain the safe default source.

ALTER TABLE tool_call_events
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'api';

ALTER TABLE tool_call_events
    ADD CONSTRAINT tool_call_events_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a'));

ALTER TABLE run_decisions
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'api';

ALTER TABLE run_decisions
    ADD CONSTRAINT run_decisions_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a'));

-- usage_events is the canonical run dimension. Backfill only rows with a
-- non-empty run ID and a valid source; unmatched legacy rows keep 'api'.
UPDATE tool_call_events AS events
SET source = usage.source
FROM usage_events AS usage
WHERE events.run_id <> ''
  AND events.run_id = usage.run_id
  AND usage.source IN ('api', 'schedule', 'trigger', 'a2a');

UPDATE run_decisions AS decisions
SET source = usage.source
FROM usage_events AS usage
WHERE decisions.run_id <> ''
  AND decisions.run_id = usage.run_id
  AND usage.source IN ('api', 'schedule', 'trigger', 'a2a');

CREATE INDEX IF NOT EXISTS idx_tool_call_events_source_created
    ON tool_call_events (source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_run_decisions_source_created
    ON run_decisions (source, created_at DESC);

COMMENT ON COLUMN tool_call_events.source IS
    'Run origin: api, schedule, trigger, or a2a; copied from usage_events for ML analysis.';

COMMENT ON COLUMN run_decisions.source IS
    'Run origin: api, schedule, trigger, or a2a; copied from usage_events for ML analysis.';

-- Source migration: 085_agent_commerce_discoverability
-- 085: Agent-commerce discoverability metadata.
-- Domain: marketplace
-- Invariant: Additive, non-financial metadata only; no money-path changes.
--
-- Adds commerce-facing description and search tags to platform tools, and
-- search tags to org tools, so agent discovery surfaces can rank and match
-- tools by intent rather than exact-name substring only.

ALTER TABLE marketplace_platform_tools
    ADD COLUMN IF NOT EXISTS marketplace_description TEXT NOT NULL DEFAULT '';

ALTER TABLE marketplace_platform_tools
    ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_org_tools_marketplace_tags_gin
    ON org_tools USING gin (tags)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_platform_tools_marketplace_tags_gin
    ON marketplace_platform_tools USING gin (tags)
    WHERE is_active = TRUE;

-- Commerce-facing descriptions for flagship platform tools. These are
-- distinct from the technical `description` and are what discovery surfaces
-- (llms.txt, catalog) expose to agents evaluating a purchase.
UPDATE marketplace_platform_tools
SET marketplace_description = 'Real-time web search for current events, fact-checking, and research. Choose when the task needs external or up-to-date information.'
WHERE tool_name = 'web_search' AND marketplace_description = '';

UPDATE marketplace_platform_tools
SET marketplace_description = 'Aggregated token holdings with USD values for a wallet on Ethereum or Base. One call for portfolio exposure and risk context per chain.'
WHERE tool_name = 'get_wallet_portfolio' AND marketplace_description = '';

UPDATE marketplace_platform_tools
SET marketplace_description = 'Best-execution Uniswap v3 swap quote on Ethereum or Base. Use before executing a trade for price impact and output amount.'
WHERE tool_name = 'get_dex_quote' AND marketplace_description = '';

-- Search tags mirror the ToolDefinition tags so catalog search can match
-- intent keywords (e.g. "swap", "risk", "portfolio") beyond the name.
UPDATE marketplace_platform_tools
SET tags = ARRAY['search', 'web', 'realtime']
WHERE tool_name = 'web_search' AND tags = '{}';

UPDATE marketplace_platform_tools
SET tags = ARRAY['web3', 'ethereum', 'portfolio', 'balance', 'defi']
WHERE tool_name = 'get_wallet_portfolio' AND tags = '{}';

UPDATE marketplace_platform_tools
SET tags = ARRAY['web3', 'defi', 'uniswap', 'dex', 'quote', 'trading']
WHERE tool_name = 'get_dex_quote' AND tags = '{}';

-- Source migration: 086_event_trigger_a2a_control_plane
-- Migration 086: A2A event-trigger task lookup and immutable operational audit.
-- Domain: A2A / scheduling
-- Invariant: Additive schema only; audit rows survive trigger deletion.

CREATE INDEX IF NOT EXISTS idx_scheduled_run_results_org_schedule_run
    ON scheduled_run_results (org_id, schedule_id, run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS event_trigger_events (
    id          TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    org_id      TEXT NOT NULL,
    run_id      TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL CHECK (event_type IN (
        'trigger_created',
        'trigger_updated',
        'trigger_deleted',
        'secret_rotated',
        'secret_rejected',
        'dispatch_accepted',
        'dispatch_duplicate',
        'run_settled'
    )),
    status      TEXT NOT NULL DEFAULT '',
    cost_usdc   BIGINT NOT NULL DEFAULT 0 CHECK (cost_usdc >= 0),
    error       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_trigger_events_org_created
    ON event_trigger_events (org_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_event_trigger_events_schedule_created
    ON event_trigger_events (schedule_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_event_trigger_events_run
    ON event_trigger_events (run_id)
    WHERE run_id <> '';

-- Source migration: 087_event_trigger_control_plane_hardening
-- Migration 087: multi-instance event-trigger execution leases.
-- Domain: A2A / scheduling
-- Invariant: additive schema; financial truth remains in existing ledgers.

CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_run_results_schedule_run
    ON scheduled_run_results (schedule_id, run_id);

CREATE TABLE IF NOT EXISTS event_dispatch_leases (
    run_id           TEXT PRIMARY KEY,
    schedule_id      TEXT NOT NULL,
    org_id           TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN (
        'reserved',
        'running',
        'completed',
        'failed'
    )),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    attempts         INTEGER NOT NULL DEFAULT 1 CHECK (attempts > 0),
    last_error       TEXT NOT NULL DEFAULT '' CHECK (char_length(last_error) <= 1024),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT event_dispatch_leases_dispatch_fk
        FOREIGN KEY (schedule_id, idempotency_key)
        REFERENCES event_dispatch_keys (schedule_id, idempotency_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_dispatch_leases_active
    ON event_dispatch_leases (lease_expires_at, run_id)
    WHERE status IN ('reserved', 'running');

CREATE INDEX IF NOT EXISTS idx_event_dispatch_leases_org_active
    ON event_dispatch_leases (org_id, lease_expires_at, run_id)
    WHERE status IN ('reserved', 'running');

CREATE INDEX IF NOT EXISTS idx_event_dispatch_leases_terminal_created
    ON event_dispatch_leases (created_at, run_id)
    WHERE status IN ('completed', 'failed');

-- Source migration: 088_withdrawal_in_flight
-- 088: Durable in-flight settlement state for marketplace withdrawals
-- Domain: marketplace
-- Invariant: claimed earnings must never be re-selected by a later sweep; the claim
-- survives process crashes so a broadcast transfer cannot be double-paid.
--
-- tool_author_earnings.withdrawal_id links a claimed earnings row to its withdrawal,
-- letting reset release ONLY the affected rows instead of a whole org's earnings.
--
-- Status transitions for sweep-initiated withdrawals:
--   pending   → in_flight  (earnings claimed, CDP transfer in progress or ambiguous)
--   in_flight → settled    (transfer confirmed on-chain; manual complete after crash)
--   in_flight → pending    (manual reset after operator confirms NO transfer occurred)
--   in_flight → failed     (transfer definitively rejected/reverted; earnings released)

ALTER TABLE tool_author_earnings
    ADD COLUMN IF NOT EXISTS withdrawal_id TEXT REFERENCES tool_author_withdrawals(id);

CREATE INDEX IF NOT EXISTS idx_author_earnings_withdrawal_id
    ON tool_author_earnings (withdrawal_id)
    WHERE withdrawal_id IS NOT NULL;

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'tool_author_withdrawals'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE tool_author_withdrawals DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END;
$$;

ALTER TABLE tool_author_withdrawals
    ADD CONSTRAINT tool_author_withdrawals_status_check
    CHECK (status IN ('pending', 'in_flight', 'settled', 'failed', 'exhausted'));

-- Source migration: 089_debank_wallet_positions
-- 089: Publish the DeBank-backed wallet positions tool.
-- Domain: tools / marketplace
-- Invariant: base_price_usdc is atomic USDC; the seed is idempotent.
--
-- The $0.020 platform price covers the default DeBank request pair
-- (all_complex_protocol_list = 30 units + total_balance = 30 units = 60 units
-- = $0.012 at $0.0002/unit) with ~40% margin for platform orchestration.
-- Positions-only mode (include_net_worth=false) costs 30 units = $0.006.
-- The tool remains usable without marketplace mode; this row makes its
-- external provider cost discoverable and separately attributable.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_wallet_positions',
    'Wallet Positions',
    20000,
    'All-chain DeBank protocol positions, token lists, and optional net worth for an EVM wallet'
)
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 090_debank_wallet_intelligence
-- 090: Publish DeBank wallet intelligence platform tools.
-- Domain: tools / marketplace
-- Invariant: base_price_usdc is atomic USDC; seeds are idempotent.
--
-- DeBank costs 10 units ($0.002) for token_authorized_list and 15 units
-- ($0.003) for all_history_list at $0.0002/unit. Prices include platform
-- orchestration margin and remain platform-tool prices, not org marketplace
-- resale prices. DeBank resale rights require a separate Service Order.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES
    (
        'get_wallet_approvals',
        'Wallet Approvals',
        4000,
        'DeBank token authorization exposure and protocol risk flags for one chain'
    ),
    (
        'get_wallet_history',
        'Wallet History',
        6000,
        'DeBank decoded wallet transaction history with protocol, token, exchange, and gas metadata'
    )
ON CONFLICT (tool_name) DO NOTHING;

-- Source migration: 091_labeling_pipeline
-- Migration 091: generalized prediction labeling data plane.
-- Domain: non-financial ML telemetry; all definitions and result versions are immutable.
-- Invariant: labeling failures never gate billing, settlement, usage, or scheduled runs.

CREATE TABLE IF NOT EXISTS labeling_definitions (
    definition_key TEXT NOT NULL,
    definition_version INTEGER NOT NULL CHECK (definition_version > 0),
    prediction_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    target_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcome_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    parser_key TEXT NOT NULL,
    parser_version TEXT NOT NULL DEFAULT '1',
    provider_key TEXT NOT NULL,
    provider_version TEXT NOT NULL DEFAULT '1',
    scorer_key TEXT NOT NULL,
    scorer_version TEXT NOT NULL DEFAULT '1',
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (definition_key, definition_version),
    CONSTRAINT labeling_definitions_key_chk
        CHECK (definition_key ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'),
    CONSTRAINT labeling_definitions_parser_chk
        CHECK (parser_key ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'),
    CONSTRAINT labeling_definitions_provider_chk
        CHECK (provider_key ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'),
    CONSTRAINT labeling_definitions_scorer_chk
        CHECK (scorer_key ~ '^[a-z0-9][a-z0-9_.-]{0,127}$')
);

CREATE TABLE IF NOT EXISTS labeling_bindings (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    definition_key TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, source_kind, source_id),
    FOREIGN KEY (definition_key, definition_version)
        REFERENCES labeling_definitions (definition_key, definition_version),
    CONSTRAINT labeling_bindings_source_kind_chk
        CHECK (source_kind IN ('scheduled_run', 'external')),
    CONSTRAINT labeling_bindings_source_id_chk CHECK (length(source_id) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS idx_labeling_bindings_org_enabled
    ON labeling_bindings (org_id, enabled);

CREATE TABLE IF NOT EXISTS labeling_predictions (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    schedule_id TEXT NOT NULL DEFAULT '',
    binding_id TEXT,
    definition_key TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    predictions JSONB NOT NULL,
    payload_sha256 TEXT NOT NULL,
    prediction_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'accepted',
    parse_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, source_kind, source_id, definition_key, definition_version),
    UNIQUE (id, org_id),
    FOREIGN KEY (definition_key, definition_version)
        REFERENCES labeling_definitions (definition_key, definition_version),
    FOREIGN KEY (binding_id)
        REFERENCES labeling_bindings (id) ON DELETE SET NULL,
    CONSTRAINT labeling_predictions_source_kind_chk
        CHECK (source_kind IN ('scheduled_run', 'external')),
    CONSTRAINT labeling_predictions_status_chk
        CHECK (status IN ('accepted', 'invalid')),
    CONSTRAINT labeling_predictions_hash_chk
        CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT labeling_predictions_run_id_chk
        CHECK (source_kind <> 'scheduled_run' OR (run_id <> '' AND schedule_id <> ''))
);

CREATE INDEX IF NOT EXISTS idx_labeling_predictions_org_created
    ON labeling_predictions (org_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_labeling_predictions_definition_created
    ON labeling_predictions (definition_key, definition_version, created_at DESC);

CREATE TABLE IF NOT EXISTS labeling_targets (
    id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    item_payload JSONB NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (prediction_id, item_key),
    UNIQUE (id, org_id),
    FOREIGN KEY (prediction_id, org_id)
        REFERENCES labeling_predictions (id, org_id) ON DELETE CASCADE,
    CONSTRAINT labeling_targets_item_key_chk CHECK (length(item_key) BETWEEN 1 AND 256),
    CONSTRAINT labeling_targets_window_chk CHECK (window_start < window_end),
    CONSTRAINT labeling_targets_status_chk
        CHECK (status IN ('pending', 'leased', 'scored', 'unavailable', 'invalid')),
    CONSTRAINT labeling_targets_lease_chk
        CHECK (
            (status = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            OR (status <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_labeling_targets_due
    ON labeling_targets (status, due_at, id);

CREATE INDEX IF NOT EXISTS idx_labeling_targets_org_due
    ON labeling_targets (org_id, status, due_at, id);

CREATE TABLE IF NOT EXISTS labeling_observations (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL DEFAULT 'public',
    provider_key TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    payload JSONB,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (scope_key, provider_key, provider_version, request_sha256),
    CONSTRAINT labeling_observations_scope_chk CHECK (length(scope_key) BETWEEN 1 AND 256),
    CONSTRAINT labeling_observations_status_chk
        CHECK (status IN ('ready', 'unavailable', 'invalid')),
    CONSTRAINT labeling_observations_hash_chk
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_labeling_observations_as_of
    ON labeling_observations (provider_key, as_of DESC);

CREATE TABLE IF NOT EXISTS labeling_results (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    scorer_key TEXT NOT NULL,
    scorer_version TEXT NOT NULL,
    observation_id TEXT,
    actual JSONB,
    label TEXT NOT NULL,
    score NUMERIC,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (target_id) REFERENCES labeling_targets (id) ON DELETE CASCADE,
    FOREIGN KEY (observation_id) REFERENCES labeling_observations (id) ON DELETE RESTRICT,
    CONSTRAINT labeling_results_status_chk
        CHECK (status IN ('correct', 'incorrect', 'neutral', 'inconclusive', 'unavailable', 'invalid')),
    CONSTRAINT labeling_results_source_chk
        CHECK (source IN ('automatic', 'external', 'manual')),
    CONSTRAINT labeling_results_score_chk
        CHECK (
            (status IN ('correct', 'incorrect', 'neutral') AND score IS NOT NULL)
            OR (status IN ('inconclusive', 'unavailable', 'invalid') AND score IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_labeling_results_target_created
    ON labeling_results (target_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_labeling_results_org_created
    ON labeling_results (created_at DESC, id DESC);

INSERT INTO tool_pricing_overrides (tool_name, cost_usdc, description)
VALUES ('record_predictions', 0, 'In-process structured prediction capture; zero marginal cost')
ON CONFLICT (tool_name) DO UPDATE
SET cost_usdc = EXCLUDED.cost_usdc,
    description = EXCLUDED.description,
    updated_at = NOW();

INSERT INTO labeling_definitions
    (definition_key, definition_version, prediction_schema, parser_key, parser_version,
     provider_key, provider_version, scorer_key, scorer_version, config)
VALUES
    (
        'entry_timing',
        1,
        '{"type":"object","required":["task_class"],"properties":{"task_class":{"const":"entry_timing"}}}'::jsonb,
        'entry_timing', '1', 'token_price', '1', 'entry_return', '1',
        '{"horizon_seconds":1209600}'::jsonb
    ),
    (
        'eth_primitive_fees',
        1,
        '{"type":"object","required":["task_class"],"properties":{"task_class":{"const":"eth_primitive_fees"}}}'::jsonb,
        'eth_protocols', '1', 'protocol_fees', '1', 'fee_direction', '1',
        '{"horizon_seconds":604800}'::jsonb
    ),
    (
        'stablecoin_yield_compare',
        1,
        '{"type":"object","required":["task_class"],"properties":{"task_class":{"const":"stablecoin_yield_compare"}}}'::jsonb,
        'stablecoin_root', '1', 'stablecoin_market', '1', 'stablecoin_spread', '1',
        '{"horizon_seconds":604800}'::jsonb
    )
ON CONFLICT (definition_key, definition_version) DO NOTHING;

-- Source migration: 092_scheduled_callback_format
-- Migration 092: optional plain-text delivery for scheduled-run callbacks.
-- Domain: scheduling output transport; JSON remains the backward-compatible default.
-- Invariant: callback formatting never changes run execution, billing, or settlement.

ALTER TABLE scheduled_runs
    ADD COLUMN IF NOT EXISTS callback_format TEXT NOT NULL DEFAULT 'json';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'scheduled_runs_callback_format_chk'
    ) THEN
        ALTER TABLE scheduled_runs
            ADD CONSTRAINT scheduled_runs_callback_format_chk
            CHECK (callback_format IN ('json', 'text'));
    END IF;
END $$;

-- Source migration: 093_a2a_delegation_refund_outbox
-- Migration 093: durable A2A delegation refund state
-- Domain: billing / delegation
-- Invariant: every funded delegation has one idempotent refund/cancel record.

CREATE TABLE IF NOT EXISTS a2a_delegation_refund_outbox (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL,
    amount_usdc BIGINT NOT NULL CHECK (amount_usdc > 0),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'refund_requested', 'refunded', 'cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    CHECK (
        (status IN ('pending', 'refund_requested') AND resolved_at IS NULL)
        OR (status IN ('refunded', 'cancelled') AND resolved_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_refund_outbox_pending
    ON a2a_delegation_refund_outbox (status, created_at)
    WHERE status = 'refund_requested';

-- Source migration: 094_a2a_inbound_tasks
-- Migration 094: asynchronous inbound A2A task state.
-- Domain: A2A / agent execution
-- Invariant: transient task state is separate from the immutable inbound audit ledger.

CREATE TABLE IF NOT EXISTS a2a_inbound_tasks (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL UNIQUE,
    client_task_id     TEXT NOT NULL DEFAULT '',
    context_id         TEXT NOT NULL DEFAULT '',
    message            JSONB NOT NULL,
    metadata           JSONB NOT NULL DEFAULT '{}'::JSONB,
    user_message       TEXT NOT NULL,
    caller_org_id      TEXT NOT NULL DEFAULT '',
    caller_user_id     TEXT NOT NULL DEFAULT '',
    caller_ip          TEXT NOT NULL DEFAULT '',
    auth_method        TEXT NOT NULL DEFAULT 'anonymous',
    task_state         TEXT NOT NULL DEFAULT 'submitted'
                       CHECK (task_state IN (
                           'submitted',
                           'running',
                           'completed',
                           'failed',
                           'timeout',
                           'rejected_payment',
                           'rejected_auth_credit'
                       )),
    output_text        TEXT NOT NULL DEFAULT ''
                       CHECK (char_length(output_text) <= 65536),
    error              TEXT NOT NULL DEFAULT ''
                       CHECK (char_length(error) <= 1024),
    usage_event_id     TEXT,
    cost_usdc          BIGINT NOT NULL DEFAULT 0 CHECK (cost_usdc >= 0),
    settlement_tx      TEXT NOT NULL DEFAULT '',
    billing_method     TEXT NOT NULL DEFAULT '',
    duration_ms        INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (char_length(client_task_id) <= 256),
    CHECK (char_length(context_id) <= 256),
    CHECK (
        (task_state IN ('submitted', 'running') AND finished_at IS NULL)
        OR (task_state IN ('completed', 'failed', 'timeout', 'rejected_payment', 'rejected_auth_credit')
            AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_caller
    ON a2a_inbound_tasks (caller_org_id, caller_user_id, created_at DESC, id DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_auth_client_id
    ON a2a_inbound_tasks (caller_org_id, caller_user_id, client_task_id)
    WHERE client_task_id <> '' AND caller_org_id <> '' AND caller_user_id <> '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_anonymous_client_id
    ON a2a_inbound_tasks (caller_ip, client_task_id)
    WHERE client_task_id <> '' AND caller_org_id = '' AND caller_user_id = '';

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_terminal_created
    ON a2a_inbound_tasks (created_at, id)
    WHERE task_state IN ('completed', 'failed', 'timeout', 'rejected_payment', 'rejected_auth_credit');

-- Source migration: 095_a2a_inbound_task_leases
-- Migration 095: multi-instance ownership and settlement projection metadata for inbound A2A tasks.
-- Domain: A2A / agent execution
-- Invariant: task leases coordinate process ownership; financial truth remains in billing ledgers.

ALTER TABLE a2a_inbound_tasks
    ADD COLUMN IF NOT EXISTS worker_owner_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS settlement_amount_usdc BIGINT NOT NULL DEFAULT 0;

ALTER TABLE a2a_inbound_tasks
    ADD CONSTRAINT a2a_inbound_tasks_settlement_amount_nonnegative
    CHECK (settlement_amount_usdc >= 0);

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_active_lease
    ON a2a_inbound_tasks (lease_expires_at, id)
    WHERE task_state IN ('submitted', 'running');

ALTER TABLE a2a_inbound_events
    ADD COLUMN IF NOT EXISTS settlement_amount_usdc BIGINT NOT NULL DEFAULT 0;

ALTER TABLE a2a_inbound_events
    ADD CONSTRAINT a2a_inbound_events_settlement_amount_nonnegative
    CHECK (settlement_amount_usdc >= 0);

-- Source migration: 096_a2a_delegation_delivery_state
-- Migration 096: durable A2A delivery ambiguity state
-- Domain: A2A / billing / x402
-- Invariant: ambiguous paid sends are held for explicit reconciliation; the
-- immutable delegation event ledger remains append-only.

ALTER TABLE a2a_delegation_refund_outbox
    ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'not_attempted'
        CHECK (delivery_status IN ('not_attempted', 'possibly_delivered', 'confirmed', 'failed')),
    ADD COLUMN IF NOT EXISTS delivery_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_resolved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_settlement_tx TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS delivery_error TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_refund_outbox_delivery_review
    ON a2a_delegation_refund_outbox (org_id, created_at DESC)
    WHERE delivery_status = 'possibly_delivered';

COMMENT ON COLUMN a2a_delegation_refund_outbox.delivery_status IS
    'Mutable delivery projection; possibly_delivered requires operator reconciliation before refund.';
COMMENT ON COLUMN a2a_delegation_refund_outbox.delivery_settlement_tx IS
    'Validated bounded x402 settlement transaction identifier, when supplied by the remote response.';

-- Source migration: 097_machine_org_provisioning
-- 097: Machine org provisioning — audit ledger + idempotent external topups.
-- Invariant: orgs.acquisition_source IN ('siwe','x402') is the sole source of
-- truth for "machine-provisioned". It is set server-side only; /register must
-- reject client-supplied values in that set.

ALTER TABLE org_credit_ledger ADD COLUMN IF NOT EXISTS external_ref TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_org_credit_ledger_external_ref
    ON org_credit_ledger (external_ref)
    WHERE external_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS org_provisioning_events (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES orgs(id),
    method            TEXT NOT NULL,
    payer_address     TEXT NOT NULL,
    chain_id          INTEGER NOT NULL,
    settlement_tx     TEXT NOT NULL DEFAULT '',
    payment_ref       TEXT,
    amount_usdc       BIGINT NOT NULL DEFAULT 0 CHECK (amount_usdc >= 0),
    event_type        TEXT NOT NULL DEFAULT 'provisioned'
                      CHECK (event_type IN ('provisioned', 'settlement')),
    settlement_status TEXT NOT NULL DEFAULT 'not_applicable'
                      CHECK (settlement_status IN ('not_applicable', 'pending', 'settled', 'failed', 'ambiguous', 'credit_failed')),
    settlement_error  TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS payment_ref TEXT;
ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS amount_usdc BIGINT NOT NULL DEFAULT 0;
ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'provisioned';
ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS settlement_status TEXT NOT NULL DEFAULT 'not_applicable';
ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS settlement_error TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_org_provisioning_events_payment_ref
    ON org_provisioning_events (payment_ref)
    WHERE payment_ref IS NOT NULL AND event_type = 'provisioned';

CREATE INDEX IF NOT EXISTS idx_org_provisioning_events_org
    ON org_provisioning_events (org_id);
CREATE INDEX IF NOT EXISTS idx_org_provisioning_events_payer
    ON org_provisioning_events (payer_address);
CREATE INDEX IF NOT EXISTS idx_org_provisioning_events_created_at
    ON org_provisioning_events (created_at);
CREATE INDEX IF NOT EXISTS idx_org_provisioning_events_payment_ref
    ON org_provisioning_events (payment_ref)
    WHERE payment_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orgs_acquisition_source ON orgs (acquisition_source);

-- Preserve provenance for machine wallets created before migration 097.
INSERT INTO org_provisioning_events
        (id, org_id, method, payer_address, chain_id, amount_usdc,
         event_type, settlement_status)
SELECT md5('097:legacy:' || w.id),
             w.org_id,
             o.acquisition_source,
             w.address,
             w.chain_id,
             0,
             'provisioned',
             'not_applicable'
FROM wallets AS w
JOIN orgs AS o ON o.id = w.org_id
WHERE o.acquisition_source IN ('siwe', 'x402')
    AND NOT EXISTS (
            SELECT 1
            FROM org_provisioning_events AS e
            WHERE e.id = md5('097:legacy:' || w.id)
    );

-- Credit rows are created with the configured machine-org cap by
-- provision_org_for_wallet; this migration does not rewrite existing limits.

-- Source migration: 098_credit_ledger_principal
-- Migration 098: attribute credit ledger debits to an authenticated principal
-- Domain: billing
-- Invariant: Additive column only; principal attribution never changes debit amounts or ledger balances
--
-- Records which authenticated principal (user or M2M credential) caused each
-- credit ledger debit, enabling per-principal spend reporting. The partial
-- index backs per-principal 24h spend aggregates without touching the
-- append-only audit trail.

ALTER TABLE org_credit_ledger
    ADD COLUMN IF NOT EXISTS principal_id TEXT;

CREATE INDEX IF NOT EXISTS idx_credit_ledger_principal_debits
    ON org_credit_ledger (org_id, principal_id, created_at DESC)
    WHERE operation = 'debit' AND principal_id IS NOT NULL;

-- Source migration: 099_org_principal_spend_limits
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

-- Source migration: 100_pending_settlement_principal
-- Migration 100: preserve credit principal attribution across settlement retries
-- Domain: billing
-- Invariant: Additive column; principal attribution survives retry-queue re-enqueue; settlement amounts unchanged
--
-- pending_settlements rows are re-enqueued on failure; carrying principal_id
-- forward keeps per-principal spend attribution intact across retries.

ALTER TABLE pending_settlements
    ADD COLUMN IF NOT EXISTS principal_id TEXT;

-- Source migration: 101_a2a_agent_registry
-- Migration 101: A2A agent registry (per-org remote agent endpoints)
-- Domain: A2A / delegation
-- Invariant: agent_url must be https-only (SSRF-safe); registry is per-org; failure_origin is additive telemetry
--
-- Registers each org's remote A2A agent endpoint so inbound delegation can be
-- routed to the correct agent URL. Also adds failure_origin to
-- a2a_delegation_events so operators can distinguish local vs remote failures.

CREATE TABLE IF NOT EXISTS a2a_agent_registry (
    org_id      TEXT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    agent_url   TEXT NOT NULL UNIQUE CHECK (agent_url ~ '^https://[^[:space:]]+$'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE a2a_delegation_events
    ADD COLUMN IF NOT EXISTS failure_origin TEXT NOT NULL DEFAULT 'unknown'
        CHECK (failure_origin IN ('unknown', 'local', 'remote'));

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_events_agent_url_created
    ON a2a_delegation_events (rtrim(agent_url, '/'), created_at DESC);
