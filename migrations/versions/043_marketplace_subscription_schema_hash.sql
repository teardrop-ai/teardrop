-- Migration 043: Add subscribed_schema_hash to org_marketplace_subscriptions.
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
