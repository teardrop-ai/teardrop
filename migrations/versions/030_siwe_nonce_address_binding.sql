-- 030_siwe_nonce_address_binding.sql
-- Defense-in-depth: bind SIWE nonces to the verified wallet address at
-- consumption time.  Nullable for backward compatibility with pre-existing
-- nonces that have no address.

ALTER TABLE siwe_nonces ADD COLUMN IF NOT EXISTS address TEXT;
