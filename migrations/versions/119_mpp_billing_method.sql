-- Migration 119: allow billing_method 'mpp' on MCP outcome ledgers.
-- Domain: billing
-- Invariants:
--   * Additive only: widens the billing_method CHECK on mcp_call_events and billing_charges;
--     existing 'x402'/'credit' rows remain valid and no data is rewritten.
--   * MPP charges are chain-confirmed before execution, so they are recorded 'settled' with the
--     payer's transaction hash; pending_settlements is untouched (MPP never enters recovery).

DO $$
DECLARE
    target_table TEXT;
    constraint_name TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['mcp_call_events', 'billing_charges']
    LOOP
        FOR constraint_name IN
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = target_table::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%billing_method%'
        LOOP
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', target_table, constraint_name);
        END LOOP;
    END LOOP;
END;
$$;

ALTER TABLE mcp_call_events
    ADD CONSTRAINT mcp_call_events_billing_method_check
    CHECK (billing_method IN ('x402', 'credit', 'mpp'));

ALTER TABLE billing_charges
    ADD CONSTRAINT billing_charges_billing_method_check
    CHECK (billing_method IN ('credit', 'x402', 'mpp'));
