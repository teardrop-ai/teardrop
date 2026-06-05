-- Migration 060: Replace unconditional UNIQUE(org_id, name) with a partial
-- unique index so that soft-deleted (is_active=FALSE) tools do not block
-- creation of a new active tool with the same name.
-- Domain: tools
-- Invariant: Only one active tool per org may bear a given name; deleted/paused
-- tools release their name for immediate reuse.

-- Safety check: there must be no active duplicates before we swap the constraint.
DO 40
DECLARE
    dup_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO dup_count
    FROM (
        SELECT org_id, name
        FROM org_tools
        WHERE is_active = TRUE
        GROUP BY org_id, name
        HAVING COUNT(*) > 1
    ) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'Cannot drop UNIQUE constraint: % active duplicate(s) exist', dup_count;
    END IF;
END 40;

-- Drop the table-level UNIQUE constraint (auto-named org_tools_org_id_name_key).
ALTER TABLE org_tools DROP CONSTRAINT IF EXISTS org_tools_org_id_name_key;

-- Add a partial unique index: two active tools in the same org cannot share a name.
-- Deleted/paused tools (is_active=FALSE) are ignored by this index, so their names
-- are immediately freed for reuse.
CREATE UNIQUE INDEX IF NOT EXISTS org_tools_org_id_name_active_uq
    ON org_tools (org_id, name)
    WHERE is_active = TRUE;

COMMENT ON INDEX org_tools_org_id_name_active_uq IS
    'Enforces unique tool names per org for active tools only; soft-deleted names are reusable.';
