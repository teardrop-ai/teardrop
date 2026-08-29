-- 100: Preserve credit principal attribution across settlement retries.

ALTER TABLE pending_settlements
    ADD COLUMN IF NOT EXISTS principal_id TEXT;