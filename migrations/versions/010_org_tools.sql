-- Migration 010: per-org custom tools
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
