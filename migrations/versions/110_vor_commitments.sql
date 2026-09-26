-- Migration 110: verified-outcome commitments anchored on Base.
-- Domain: labeling / verified-outcome record
-- Invariants:
--   * commitment_batches is source-agnostic. Any anchorable table carries
--     leaf_sha256, commit_salt, anchor_batch_id, and anchor_leaf_index plus its
--     own guard trigger (see labeling/commitments.py).
--   * Committed rows are immutable and never deleted; anchor fields are set once.
--   * A batch is frozen once its anchor transaction is confirmed on-chain.
-- Additive only: existing predictions keep NULL commitment columns and remain
-- subject to labeling retention.

CREATE TABLE IF NOT EXISTS commitment_batches (
    id               TEXT PRIMARY KEY,
    merkle_root      TEXT NOT NULL,
    leaf_count       INTEGER NOT NULL,
    chain_id         INTEGER NOT NULL,
    tx_hash          TEXT,
    anchor_address   TEXT,
    block_number     BIGINT,
    anchored_at      TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error       TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT commitment_batches_root_chk CHECK (merkle_root ~ '^[0-9a-f]{64}$'),
    CONSTRAINT commitment_batches_leaf_count_chk CHECK (leaf_count BETWEEN 1 AND 4096),
    CONSTRAINT commitment_batches_tx_chk CHECK (tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{64}$'),
    CONSTRAINT commitment_batches_address_chk
        CHECK (anchor_address IS NULL OR anchor_address ~ '^0x[0-9a-f]{40}$'),
    CONSTRAINT commitment_batches_submission_chk CHECK ((tx_hash IS NULL) = (anchor_address IS NULL)),
    CONSTRAINT commitment_batches_confirmation_chk CHECK ((block_number IS NULL) = (anchored_at IS NULL)),
    CONSTRAINT commitment_batches_confirmed_tx_chk CHECK (block_number IS NULL OR tx_hash IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_commitment_batches_unconfirmed
    ON commitment_batches (created_at, id)
    WHERE block_number IS NULL;

ALTER TABLE labeling_predictions
    ADD COLUMN IF NOT EXISTS signer_address TEXT,
    ADD COLUMN IF NOT EXISTS signature TEXT,
    ADD COLUMN IF NOT EXISTS commit_salt TEXT,
    ADD COLUMN IF NOT EXISTS leaf_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS anchor_batch_id TEXT REFERENCES commitment_batches (id),
    ADD COLUMN IF NOT EXISTS anchor_leaf_index INTEGER;

ALTER TABLE labeling_predictions DROP CONSTRAINT IF EXISTS labeling_predictions_signer_chk;
ALTER TABLE labeling_predictions
    ADD CONSTRAINT labeling_predictions_signer_chk CHECK (
        (signer_address IS NULL) = (signature IS NULL)
        AND (
            signer_address IS NULL
            OR (signer_address ~ '^0x[0-9a-f]{40}$' AND signature ~ '^0x[0-9a-f]{130}$')
        )
    );

-- NOT VALID keeps deployment safe if a legacy external row exists; new rows are enforced.
ALTER TABLE labeling_predictions DROP CONSTRAINT IF EXISTS labeling_predictions_external_signed_chk;
ALTER TABLE labeling_predictions
    ADD CONSTRAINT labeling_predictions_external_signed_chk CHECK (
        source_kind <> 'external'
        OR (signer_address IS NOT NULL AND leaf_sha256 IS NOT NULL)
    ) NOT VALID;

ALTER TABLE labeling_predictions DROP CONSTRAINT IF EXISTS labeling_predictions_leaf_chk;
ALTER TABLE labeling_predictions
    ADD CONSTRAINT labeling_predictions_leaf_chk CHECK (
        (leaf_sha256 IS NULL) = (commit_salt IS NULL)
        AND (
            leaf_sha256 IS NULL
            OR (
                status = 'accepted'
                AND leaf_sha256 ~ '^[0-9a-f]{64}$'
                AND commit_salt ~ '^[0-9a-f]{64}$'
            )
        )
    );

ALTER TABLE labeling_predictions DROP CONSTRAINT IF EXISTS labeling_predictions_anchor_chk;
ALTER TABLE labeling_predictions
    ADD CONSTRAINT labeling_predictions_anchor_chk CHECK (
        (anchor_batch_id IS NULL) = (anchor_leaf_index IS NULL)
        AND (anchor_batch_id IS NULL OR (leaf_sha256 IS NOT NULL AND anchor_leaf_index >= 0))
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_labeling_predictions_anchor_leaf
    ON labeling_predictions (anchor_batch_id, anchor_leaf_index)
    WHERE anchor_batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_labeling_predictions_unanchored
    ON labeling_predictions (prediction_at, id)
    WHERE leaf_sha256 IS NOT NULL AND anchor_batch_id IS NULL;

CREATE OR REPLACE FUNCTION vor_guard_prediction() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.leaf_sha256 IS NULL THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'committed prediction % is append-only', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.id, NEW.org_id, NEW.source_kind, NEW.source_id, NEW.definition_key, NEW.definition_version,
        NEW.predictions, NEW.payload_sha256, NEW.prediction_at, NEW.status,
        NEW.signer_address, NEW.signature, NEW.commit_salt, NEW.leaf_sha256)
       IS DISTINCT FROM
       (OLD.id, OLD.org_id, OLD.source_kind, OLD.source_id, OLD.definition_key, OLD.definition_version,
        OLD.predictions, OLD.payload_sha256, OLD.prediction_at, OLD.status,
        OLD.signer_address, OLD.signature, OLD.commit_salt, OLD.leaf_sha256) THEN
        RAISE EXCEPTION 'committed prediction % is immutable', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.anchor_batch_id IS NOT NULL
       AND (NEW.anchor_batch_id, NEW.anchor_leaf_index) IS DISTINCT FROM (OLD.anchor_batch_id, OLD.anchor_leaf_index) THEN
        RAISE EXCEPTION 'prediction % is already anchored', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_vor_guard_prediction ON labeling_predictions;
CREATE TRIGGER trg_vor_guard_prediction
    BEFORE UPDATE OR DELETE ON labeling_predictions
    FOR EACH ROW EXECUTE FUNCTION vor_guard_prediction();

CREATE OR REPLACE FUNCTION vor_guard_batch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'commitment batch % is append-only', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.id, NEW.merkle_root, NEW.leaf_count, NEW.chain_id, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.merkle_root, OLD.leaf_count, OLD.chain_id, OLD.created_at) THEN
        RAISE EXCEPTION 'commitment batch % root is immutable', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.block_number IS NOT NULL
       AND (NEW.tx_hash, NEW.anchor_address, NEW.block_number, NEW.anchored_at)
           IS DISTINCT FROM (OLD.tx_hash, OLD.anchor_address, OLD.block_number, OLD.anchored_at) THEN
        RAISE EXCEPTION 'commitment batch % is confirmed', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_vor_guard_batch ON commitment_batches;
CREATE TRIGGER trg_vor_guard_batch
    BEFORE UPDATE OR DELETE ON commitment_batches
    FOR EACH ROW EXECUTE FUNCTION vor_guard_batch();

COMMENT ON TABLE commitment_batches IS
    'Source-agnostic RFC 6962 Merkle batches of committed leaves, anchored as 0-value self-transactions on Base.';
COMMENT ON COLUMN labeling_predictions.leaf_sha256 IS
    'Versioned commitment leaf (v1 = prediction); set once at insert when commitments are enabled.';
