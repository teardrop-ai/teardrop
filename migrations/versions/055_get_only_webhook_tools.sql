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
