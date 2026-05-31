-- Migration 059: x402 payment-header replay guard (concurrent-replay protection)
-- Domain: billing
-- Invariant: PRIMARY KEY on nonce_hash makes the first INSERT win; concurrent
-- replays of the same signed payment header are rejected before the tool runs.
--
-- The blockchain already prevents an EIP-3009 authorization from settling
-- twice on-chain. This table additionally closes the *concurrent* window where
-- two in-flight requests carrying the identical payment header both pass
-- verification and both execute the paid tool before either settles.
--
-- nonce_hash is the SHA-256 of the raw base64 payment header. claimed_at drives
-- the 24h retention sweep (a fresh authorization is required after that anyway).

CREATE TABLE IF NOT EXISTS x402_payment_nonces (
    nonce_hash  TEXT PRIMARY KEY,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_x402_payment_nonces_claimed_at
    ON x402_payment_nonces (claimed_at);
