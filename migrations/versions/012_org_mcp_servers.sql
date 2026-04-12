-- Migration 012: Per-org MCP server connections
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
