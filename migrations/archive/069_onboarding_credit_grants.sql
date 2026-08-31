-- Migration 069: verified-email onboarding credit grants and retry outbox
-- Domain: billing / onboarding
-- Invariant: one immutable grant marker per organisation; balance and ledger
-- mutation is performed in the same transaction as the marker insert.

CREATE TABLE IF NOT EXISTS org_onboarding_credit_grants (
    org_id          TEXT        PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    amount_usdc     BIGINT      NOT NULL CHECK (amount_usdc > 0),
    ledger_entry_id TEXT        NOT NULL UNIQUE REFERENCES org_credit_ledger(id) DEFERRABLE INITIALLY DEFERRED,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS org_onboarding_credit_outbox (
    org_id          TEXT        PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    amount_usdc     BIGINT      NOT NULL CHECK (amount_usdc > 0),
    attempts        INTEGER     NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);