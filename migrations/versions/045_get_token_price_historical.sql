-- 045: Add get_token_price_historical to the marketplace platform tools catalog.
--
-- Historical crypto price tool wrapping CoinGecko /coins/{id}/market_chart.
-- Returns period statistics (start, end, % change, high, low) plus a
-- downsampled daily price series for windows of 1–365 days. Eliminates the
-- web_search loop that previously occurred on every time-comparative query.
-- Priced at $0.004 (4,000 atomic USDC) per call — one upstream API request
-- per token, matching get_wallet_portfolio's per-call cost profile.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_token_price_historical',
    'Token Price History',
    4000,
    'Historical crypto price data via CoinGecko — period stats and daily series for 1–365 day windows'
)
ON CONFLICT (tool_name) DO NOTHING;
