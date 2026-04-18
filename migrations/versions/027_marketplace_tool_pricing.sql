-- 027: Add author-controlled per-tool pricing to org_tools.
--
-- Allows tool authors to set a base price for each published tool.
-- 0 = use platform default pricing.  Max $100.00 (100_000_000 atomic USDC).

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS base_price_usdc BIGINT NOT NULL DEFAULT 0
    CHECK (base_price_usdc >= 0 AND base_price_usdc <= 100000000);

COMMENT ON COLUMN org_tools.base_price_usdc IS
    'Author-set per-call price in atomic USDC (6 decimals). 0 = platform default.';
