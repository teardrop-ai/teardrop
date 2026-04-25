-- Migration 042: Add schema_hash and last_schema_changed_at to org_tools.
--
-- schema_hash is a GENERATED STORED column computed as md5(input_schema::text).
-- JSONB-to-text cast is deterministic (Postgres normalises key ordering in the
-- binary representation), so the hash is consistent across reads and matches
-- the value computed server-side at subscription time.
--
-- last_schema_changed_at records when the input_schema was last modified.
-- Initialised to created_at for existing rows; updated by application code
-- whenever input_schema is mutated.

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS schema_hash TEXT
        GENERATED ALWAYS AS (md5(input_schema::text)) STORED;

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS last_schema_changed_at TIMESTAMPTZ;

-- Back-fill: use created_at as the baseline for all existing rows.
UPDATE org_tools
SET last_schema_changed_at = created_at
WHERE last_schema_changed_at IS NULL;
