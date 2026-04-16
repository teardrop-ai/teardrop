-- Per-org LLM configuration for multi-model gateway + BYOK support.

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
