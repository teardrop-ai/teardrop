-- Add persisted output schema contract for webhook tools.
ALTER TABLE org_tools
ADD COLUMN IF NOT EXISTS output_schema JSONB;

-- Keep drift-detection support parallel to existing input schema hash.
ALTER TABLE org_tools
ADD COLUMN IF NOT EXISTS output_schema_hash TEXT
GENERATED ALWAYS AS (
    CASE
        WHEN output_schema IS NOT NULL THEN md5(output_schema::text)
        ELSE NULL
    END
) STORED;
