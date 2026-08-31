-- 097: Machine org provisioning — audit ledger + idempotent external topups.
-- Invariant: orgs.acquisition_source IN ('siwe','x402') is the sole source of
-- truth for "machine-provisioned". It is set server-side only; /register must
-- reject client-supplied values in that set.

ALTER TABLE org_credit_ledger ADD COLUMN IF NOT EXISTS external_ref TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_org_credit_ledger_external_ref
    ON org_credit_ledger (external_ref)
    WHERE external_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS org_provisioning_events (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES orgs(id),
    method            TEXT NOT NULL,
    payer_address     TEXT NOT NULL,
    chain_id          INTEGER NOT NULL,
    settlement_tx     TEXT NOT NULL DEFAULT '',
    payment_ref       TEXT,
    amount_usdc       BIGINT NOT NULL DEFAULT 0 CHECK (amount_usdc >= 0),
    event_type        TEXT NOT NULL DEFAULT 'provisioned'
                      CHECK (event_type IN ('provisioned', 'settlement')),
    settlement_status TEXT NOT NULL DEFAULT 'not_applicable'
                      CHECK (settlement_status IN ('not_applicable', 'pending', 'settled', 'failed', 'ambiguous', 'credit_failed')),
    settlement_error  TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS payment_ref TEXT;
ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS amount_usdc BIGINT NOT NULL DEFAULT 0;
ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'provisioned';
ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS settlement_status TEXT NOT NULL DEFAULT 'not_applicable';
ALTER TABLE org_provisioning_events ADD COLUMN IF NOT EXISTS settlement_error TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_org_provisioning_events_payment_ref
    ON org_provisioning_events (payment_ref)
    WHERE payment_ref IS NOT NULL AND event_type = 'provisioned';

CREATE INDEX IF NOT EXISTS idx_org_provisioning_events_org
    ON org_provisioning_events (org_id);
CREATE INDEX IF NOT EXISTS idx_org_provisioning_events_payer
    ON org_provisioning_events (payer_address);
CREATE INDEX IF NOT EXISTS idx_org_provisioning_events_created_at
    ON org_provisioning_events (created_at);
CREATE INDEX IF NOT EXISTS idx_org_provisioning_events_payment_ref
    ON org_provisioning_events (payment_ref)
    WHERE payment_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orgs_acquisition_source ON orgs (acquisition_source);

-- Preserve provenance for machine wallets created before migration 097.
INSERT INTO org_provisioning_events
        (id, org_id, method, payer_address, chain_id, amount_usdc,
         event_type, settlement_status)
SELECT md5('097:legacy:' || w.id),
             w.org_id,
             o.acquisition_source,
             w.address,
             w.chain_id,
             0,
             'provisioned',
             'not_applicable'
FROM wallets AS w
JOIN orgs AS o ON o.id = w.org_id
WHERE o.acquisition_source IN ('siwe', 'x402')
    AND NOT EXISTS (
            SELECT 1
            FROM org_provisioning_events AS e
            WHERE e.id = md5('097:legacy:' || w.id)
    );

-- Credit rows are created with the configured machine-org cap by
-- provision_org_for_wallet; this migration does not rewrite existing limits.
