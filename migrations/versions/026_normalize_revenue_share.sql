-- Migration 026: Normalize revenue_share_bps to default
-- Domain: marketplace
-- Invariant: All authors use the platform default 7000 bps (70/30 author/platform split)
--
-- Resets all tool_author_config.revenue_share_bps values to the platform default (7000 = 70%).
-- The column is NOT dropped — it is preserved for future automatic volume-based tier systems.
--
-- Context: Per-author revenue_share_bps overrides are no longer supported by the application.
-- All authors now receive the fixed platform default (70/30 split). The code no longer reads
-- or writes revenue_share_bps, but the DB column is retained for forward compatibility.

UPDATE tool_author_config
SET revenue_share_bps = 7000
WHERE revenue_share_bps IS NOT NULL AND revenue_share_bps != 7000;
