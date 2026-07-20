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