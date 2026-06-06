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