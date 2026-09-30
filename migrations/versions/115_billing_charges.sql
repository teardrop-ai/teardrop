-- Migration 115: unified billing charge ledger (dual-write phase).
-- Domain: billing
-- Invariants:
--   * One row per billed invocation, unique on (source, invocation_id); amounts are BIGINT atomic USDC.
--   * Charge identity and quoted amount are immutable; rows are never deleted.
--   * Status may only move failed -> settled (credit retry) or settled -> reverted (on-chain receipt check).
--   * Payment payloads and tool arguments are never stored.
-- Additive only; existing revenue reads are unchanged until reconciliation passes.

CREATE TABLE IF NOT EXISTS billing_charges (
    id                  TEXT        PRIMARY KEY,
    source              TEXT        NOT NULL
                                    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a', 'mcp', 'mcp_v1')),
    invocation_id       TEXT        NOT NULL,
    usage_event_id      TEXT        NOT NULL DEFAULT '',
    org_id              TEXT        NOT NULL DEFAULT '',
    principal_id        TEXT        NOT NULL DEFAULT '',
    payer_address       TEXT        NOT NULL DEFAULT '',
    capability          TEXT        NOT NULL DEFAULT '',
    billing_method      TEXT        NOT NULL CHECK (billing_method IN ('credit', 'x402')),
    amount_usdc         BIGINT      NOT NULL CHECK (amount_usdc >= 0),
    settled_amount_usdc BIGINT      NOT NULL DEFAULT 0 CHECK (settled_amount_usdc >= 0),
    status              TEXT        NOT NULL CHECK (status IN ('settled', 'failed', 'reverted')),
    settlement_tx       TEXT        NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_billing_charges_invocation UNIQUE (source, invocation_id)
);

CREATE INDEX IF NOT EXISTS idx_billing_charges_created
    ON billing_charges (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_billing_charges_usage_event
    ON billing_charges (usage_event_id)
    WHERE usage_event_id <> '';

CREATE OR REPLACE FUNCTION billing_charges_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'billing charge % is append-only', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.id, NEW.source, NEW.invocation_id, NEW.usage_event_id, NEW.org_id, NEW.principal_id,
        NEW.payer_address, NEW.capability, NEW.billing_method, NEW.amount_usdc, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.source, OLD.invocation_id, OLD.usage_event_id, OLD.org_id, OLD.principal_id,
        OLD.payer_address, OLD.capability, OLD.billing_method, OLD.amount_usdc, OLD.created_at) THEN
        RAISE EXCEPTION 'billing charge % identity is immutable', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    IF NOT (
        (OLD.status = 'failed' AND NEW.status = 'settled')
        OR (OLD.status = 'settled' AND NEW.status = 'reverted'
            AND NEW.settled_amount_usdc = OLD.settled_amount_usdc
            AND NEW.settlement_tx = OLD.settlement_tx)
    ) THEN
        RAISE EXCEPTION 'billing charge % cannot move from % to %', OLD.id, OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_billing_charges_guard ON billing_charges;
CREATE TRIGGER trg_billing_charges_guard
    BEFORE UPDATE OR DELETE ON billing_charges
    FOR EACH ROW EXECUTE FUNCTION billing_charges_guard();

ALTER TABLE pending_settlements
    ADD COLUMN IF NOT EXISTS charge_id TEXT NOT NULL DEFAULT '';

COMMENT ON TABLE billing_charges IS
    'Unified per-invocation charge ledger across agent runs and MCP calls; excludes payment payloads and tool arguments.';
