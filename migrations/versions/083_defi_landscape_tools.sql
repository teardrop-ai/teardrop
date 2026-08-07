-- 083: Add chain-health and DEX-volume analysis tools to the platform catalog.
-- Domain: tools
-- Invariant: base_price_usdc BIGINT atomic USDC
--
-- Both tools use bounded DeFiLlama aggregate requests and are priced at
-- $0.003 (3,000 atomic USDC), matching get_protocol_tvl.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES
    (
        'get_chain_metrics',
        'Chain Metrics',
        3000,
        'Blockchain ecosystem health from DeFiLlama — current TVL, 7d/30d TVL trend, and aggregate fees'
    ),
    (
        'get_dex_volume',
        'DEX Volume',
        3000,
        'DEX landscape activity from DeFiLlama — 24h/7d/30d volume, changes, and global volume share'
    )
ON CONFLICT (tool_name) DO NOTHING;