-- Migration 011: org memories (persistent agent memory / RAG per org)
-- Enables pgvector extension and creates org_memories table for storing
-- per-org embedding-backed factual memories recalled during agent runs.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS org_memories (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    content       TEXT NOT NULL CHECK (length(content) <= 500),
    embedding     VECTOR(1536) NOT NULL,
    source_run_id TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for fast cosine similarity search within an org.
CREATE INDEX IF NOT EXISTS idx_org_memories_embedding
    ON org_memories USING hnsw (embedding vector_cosine_ops);

-- Composite index for listing / pagination by org.
CREATE INDEX IF NOT EXISTS idx_org_memories_org_created
    ON org_memories (org_id, created_at DESC);
