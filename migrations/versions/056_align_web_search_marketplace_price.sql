-- 056: Align marketplace web_search price with agent-run override pricing.
-- Domain: marketplace
-- Invariant: Marketplace price aligned to the agent-run override; BIGINT atomic USDC
--
-- Background:
-- - tool_pricing_overrides seeds web_search at 15,000 atomic USDC ($0.015)
-- - marketplace_platform_tools currently has web_search at 10,000 ($0.010)
-- This migration aligns marketplace catalog pricing to 15,000 so direct MCP
-- gateway calls and agent-run billing follow the same per-call price.
--
-- Product note: this increases direct marketplace web_search price by $0.005.

UPDATE marketplace_platform_tools
SET base_price_usdc = 15000
WHERE tool_name = 'web_search';
