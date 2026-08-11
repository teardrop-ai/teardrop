# Platform Tools Catalog

Teardrop exposes built-in, metered tools through the marketplace catalog. Callers can invoke them:
- Via the **MCP gateway** at `GET /tools/mcp` (direct tool invocation, billed per call).
- As **tools called during agent runs** (via `POST /agent/run` when the agent decides to use them; billed in the run's usage cost).

Pricing is fixed per call in atomic USDC (1,000,000 = $1.00):

| Tool | Price/call |
|------|------------|
| `get_wallet_portfolio` | $0.004 (4,000 atomic) |
| `web_search` | $0.015 (15,000 atomic) |
| `get_token_price` | $0.002 (2,000 atomic) |
| `get_token_price_historical` | $0.004 (4,000 atomic) |
| `get_protocol_tvl` | $0.003 (3,000 atomic) |
| `get_chain_metrics` | $0.003 (3,000 atomic) |
| `get_dex_volume` | $0.003 (3,000 atomic) |
| `get_yield_rates` | $0.004 (4,000 atomic) |
| `get_lending_rates` | $0.003 (3,000 atomic) |
| `http_fetch` | $0.002 (2,000 atomic) |
| `convert_currency` | $0.002 (2,000 atomic) |
| `get_eth_balance` | $0.001 (1,000 atomic) |
| `get_erc20_balance` | $0.002 (2,000 atomic) |
| `get_block` | $0.001 (1,000 atomic) |
| `get_transaction` | $0.002 (2,000 atomic) |
| `get_token_approvals` | $0.004 (4,000 atomic) |
| `get_defi_positions` | $0.013 (13,000 atomic) |
| `get_liquidation_risk` | $0.010 (10,000 atomic) |
| `get_dex_quote` | $0.005 (5,000 atomic) |
| `get_gas_price` | $0.002 (2,000 atomic) |
| `resolve_ens` | $0.003 (3,000 atomic) |

## Agent Discovery

Public A2A skills/tools and the MCP server card expose each built-in tool's
description, tags, version, input schema, and output schema. Selected tools
also publish `use_when`, `limitations`, and `alternatives` guidance to help
agents choose the right capability and avoid redundant calls.

Marketplace discovery adds a commerce-facing `marketplace_description`,
intent tags, price, health, and aggregate reputation. Browse these through
`GET /marketplace/catalog`, `GET /marketplace/llms.txt`, and
`GET /.well-known/reputation.json`. Community publishers can manage intent
tags through the authenticated org-tool API; tags are normalized and bounded
before being included in catalog search.

In-process utility tools `calculate`, `get_datetime`, and `count_text_stats` have zero marginal cost and are billed at $0.000 per call.

`get_yield_rates` supports an optional `stable_only` filter for consistency-focused stablecoin discovery and returns both `apy_mean_7d` and `apy_mean_30d` so clients can avoid short-window APY spikes. Use `max_apy` (e.g. `30`) to exclude leveraged/boosted pools so genuine stablecoin yields surface; combine with `stable_only=true` and rank by `apy_mean_30d` for durable, risk-averse screening.

`get_wallet_portfolio` tracks the major assets used by the Ethereum and Base lending/staking adapters, including `wstETH`, `cbETH`, `rETH`, `weETH`, and `cbBTC` where deployed. The same tracked-asset registry supplies the default token set for `get_token_approvals`; untracked assets remain explicitly excluded from both default scans.

DeFiLlama analytics tools `get_yield_rates`, `get_protocol_tvl`, and `get_chain_metrics` include an optional machine-readable `provenance` object. It reports the provider, source URL(s), response retrieval time, source fetch time, cache hit status, source age, and cache TTL. Cached responses preserve the original source timestamp and receive a new retrieval timestamp; failed upstream fetches leave `source_fetched_at` null.

---

## Tool Definitions

All system tool implementations are under [tools/definitions/](tools/definitions/). The following 27 tools are currently registered:

| Tool | Description |
|------|-------------|
| `calculate` | Evaluates arithmetic expressions safely (no `eval`). Supports `+`, `-`, `*`, `/`, `**`, `%`, `sqrt`, `abs`, `round`, `floor`, `ceil`, `log`, `sin`, `cos`, `tan`, `pi`, `e`. |
| `convert_currency` | Converts between fiat and crypto currencies using CoinGecko and live fiat exchange rates. |
| `decode_transaction` | Decodes transaction calldata into human-readable form using the supplied ABI or 4byte.directory. |
| `delegate_to_agent` | Delegate a task to a remote A2A-compliant agent. Discovers capabilities, sends a message, handles optional x402 payment, debits org credits, and records audit events. |
| `get_block` | Block metadata (timestamp, gas, miner, tx count) by number or `"latest"`. |
| `get_chain_metrics` | Blockchain ecosystem health via DeFiLlama: current TVL, 7d/30d TVL change, aggregate chain fees, and provenance metadata. |
| `get_datetime` | Returns current UTC date/time. Accepts an optional `strftime` format string. |
| `get_erc20_balance` | ERC-20 token balance for an address. |
| `get_eth_balance` | ETH balance for an Ethereum address (mainnet or Base). Requires `ETHEREUM_RPC_URL` or `BASE_RPC_URL`. |
| `get_gas_price` | Current gas price (gwei) and EIP-1559 fee components on Ethereum or Base. |
| `get_token_price` | Crypto asset price in USD (or any supported currency) via CoinGecko. |
| `get_transaction` | Transaction details and status by hash. |
| `get_wallet_portfolio` | Aggregated token holdings and USD value for an Ethereum or Base wallet, including major spot, lending, liquid-staking, restaking, and stablecoin assets. |
| `http_fetch` | Fetches and extracts content from a URL. Includes SSRF protection — private/cloud-metadata IPs are blocked, and every redirect hop is re-validated before being followed. |
| `read_contract` | Calls `view`/`pure` functions on any smart contract by ABI fragment, with optional `caller_address` context for `msg.sender`-dependent views. Calls are read-only and use bounded RPC timeout/retry handling. |
| `resolve_ens` | Resolves ENS name → address or address → ENS primary name. |
| `count_text_stats` | Returns character, word, sentence, and paragraph counts for a given text. |
| `web_search` | Web search via Tavily. Set `TAVILY_API_KEY` to activate. |
| `get_defi_positions` | Aggregate DeFi positions (expanded Aave v3 reserves, Compound v3 markets, Uniswap v3 LP, and canonical Ethereum Lido stETH/wstETH balances) for a wallet on Ethereum or Base. |
| `get_dex_quote` | Best Uniswap v3 swap quote across all fee tiers on Ethereum or Base via on-chain QuoterV2. |
| `get_dex_volume` | DEX landscape activity via DeFiLlama: 24h/7d/30d volume, period changes, and reported global 24h volume share. |
| `get_liquidation_risk` | Assess DeFi liquidation risk for up to 50 wallets across expanded Aave v3 and Compound v3 coverage. |
| `get_token_approvals` | Audit ERC-20 token allowances and flag risky unlimited approvals across major DeFi spenders. Returns an `error` field when the full RPC approval batch fails so consumers can treat results as incomplete instead of "clean". |
| `get_lending_rates` | Current on-chain lending supply/borrow rates for expanded Aave v3 reserves and Compound v3 markets on Ethereum or Base. Returns per-asset APY snapshots and Compound utilization for stablecoin yield comparisons. |
| `get_protocol_tvl` | Total Value Locked (TVL) for a DeFi protocol via DeFiLlama: current USD TVL, 7d/30d change, per-chain breakdown, optional daily historical series, and provenance metadata. Supports batching and 3,000+ protocols. |
| `get_token_price_historical` | Historical crypto price data via CoinGecko over a 1–365 day window. Returns period statistics, a downsampled daily series, `high_30d`, `std_30d` (population standard deviation of daily returns), and `dca_baseline_90d` from weekly samples over the preceding 90 UTC days; check `dca_baseline_90d_partial` for incomplete history. |
| `get_yield_rates` | DeFi yield pool rates from DeFiLlama across 1,000+ protocols and all chains. Returns pools sorted by APY with TVL, base/reward APY, 7d/30d mean APY context, and provenance metadata. |
