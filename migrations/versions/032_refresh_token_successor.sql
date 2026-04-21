-- Migration 032: refresh token successor column for idempotent rotation
-- Adds successor_token so that a rotated-but-undelivered refresh token can be
-- replayed within the idempotency window (see refresh_token_idempotency_window_seconds).
-- The column is self-referential: each revoked token points to its replacement.

ALTER TABLE refresh_tokens
    ADD COLUMN IF NOT EXISTS successor_token TEXT REFERENCES refresh_tokens(token) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_successor
    ON refresh_tokens (successor_token)
    WHERE successor_token IS NOT NULL;
