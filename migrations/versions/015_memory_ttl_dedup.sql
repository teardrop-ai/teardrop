-- Migration 015: memory deduplication + TTL expiry
-- Adds content_hash for exact-match dedup and expires_at for TTL enforcement.

ALTER TABLE org_memories
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

ALTER TABLE org_memories
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- Unique index on (org_id, content_hash) prevents exact-duplicate facts per org.
-- WHERE content_hash IS NOT NULL: existing rows without a hash are ignored.
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_memories_dedup
    ON org_memories (org_id, content_hash)
    WHERE content_hash IS NOT NULL;

-- Backfill content_hash for existing rows.
UPDATE org_memories
SET content_hash = encode(sha256(lower(trim(content))::bytea), 'hex')
WHERE content_hash IS NULL;

-- Partial index for efficient expired-memory cleanup.
CREATE INDEX IF NOT EXISTS idx_org_memories_expires
    ON org_memories (expires_at)
    WHERE expires_at IS NOT NULL;
