-- Migration 001: baseline schema
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
