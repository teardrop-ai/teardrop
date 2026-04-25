-- Migration 040: Indexes for marketplace catalog filtering and sorting.
-- Supports the new GET /marketplace/catalog?org_slug=&sort= query parameters.
-- The composite index on (publish_as_mcp, is_active, name) covers the common
-- catalog scan with ORDER BY name.  The price index covers price_asc/price_desc sorts.

CREATE INDEX IF NOT EXISTS idx_org_tools_catalog
    ON org_tools (publish_as_mcp, is_active, name)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_org_tools_catalog_price
    ON org_tools (publish_as_mcp, is_active, base_price_usdc, name)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;
