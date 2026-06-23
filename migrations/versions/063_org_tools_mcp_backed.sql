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