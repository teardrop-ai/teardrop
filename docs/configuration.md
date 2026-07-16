# Configuration Reference

This reference lists the environment variables and configuration parameters used for configuring and running a Teardrop instance in development and production environments.

Set these key-value pairs in your `.env` file or within your deployment provider dashboard (e.g., Render, Docker Compose).

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AGENT_PROVIDER` | `anthropic`, `openai`, `google`, or `openrouter` (default: `openrouter`) |
| `AGENT_MODEL` | Optional global model override (default: `deepseek/deepseek-v4-flash`). When using OpenRouter DeepSeek models, provider routing is pinned to `NovitaAI` and `DeepInfra`. |
| `AGENT_SYNTHESIS_MAX_TOKENS` | Max output tokens for post-tool synthesis turns (`tool_iterations >= 1`). Default: `4096`. |
| `AGENT_PLANNER_PROVIDER` | Optional provider override for initial planner turns (`tool_iterations==0`) when no org-level BYOK config is set. |
| `AGENT_PLANNER_MODEL` | Optional model override paired with `AGENT_PLANNER_PROVIDER` for first-pass tool selection speed tuning. |
| `AGENT_SYNTHESIS_PROVIDER` | Optional provider override for post-tool synthesis turns (`tool_iterations >= 1`). When unset, uses `AGENT_PROVIDER`. |
| `AGENT_SYNTHESIS_MODEL` | Optional model override for synthesis turns, paired with `AGENT_SYNTHESIS_PROVIDER`. |
| `AGENT_UI_GENERATOR_PROVIDER` | Provider for UI generation turns (default: `google`). **Important**: requires `GOOGLE_API_KEY` even if main provider is OpenRouter. |
| `AGENT_UI_GENERATOR_MODEL` | Model for UI generation turns (default: `gemini-3-flash-preview`). |
| `AGENT_SYNTHESIS_FAST_PATH_ENABLED` | Enables a synthesis-only fast path that skips tool schema binding when the next turn is clearly final. Default: `true`. |
| `AGENT_COMPILER_MODE_ENABLED` | Enables optional staged planner IR (`<plan>{...}</plan>`) execution. Default: `false` (safe rollout). |
| `AGENT_CACHE_PREWARM_ENABLED` | Enables one-time startup prompt-cache prewarm for top active org/provider/model prefixes. Default: `true`. |
| `AGENT_CACHE_PREWARM_TOP_N` | Max number of active prefixes warmed per startup batch. Default: `50`. |
| `AGENT_LLM_TIMEOUT_SECONDS` | Timeout in seconds for the planner LLM call (default: `180`). |
| `AGENT_TOOL_EXECUTOR_TIMEOUT_SECONDS` | Timeout in seconds for the overall tool execution node (default: `120`). |
| `AGENT_SINGLE_TOOL_TIMEOUT_SECONDS` | Per-tool deadline in seconds (default: `30`). Slow tools are converted into timeout tool messages so synthesis proceeds with partial data. |
| `ANTHROPIC_API_KEY` | Required if `AGENT_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | Required if `AGENT_PROVIDER=openai` |
| `GOOGLE_API_KEY` | Required if `AGENT_PROVIDER=google` |
| `DATABASE_URL` | Neon Postgres connection string |
| `BILLING_ENABLED` | `true` to activate x402 payments |
| `ONBOARDING_CREDIT_ENABLED` | `true` to grant prepaid credit after email verification (default: `false`) |
| `ONBOARDING_CREDIT_USDC` | Grant amount in atomic USDC, max 10,000,000 (default: `500000` = $0.50) |
| `ONBOARDING_CREDIT_RETRY_INTERVAL_SECONDS` | Poll interval for retrying failed onboarding-credit grants (default: `60`) |
| `X402_PAY_TO_ADDRESS` | Treasury wallet (USDC recipient) |
| `X402_NETWORK` | `eip155:8453` for Base mainnet |
| `X402_SCHEME` | Payment scheme: `exact` (default) or `upto` (usage-based via Permit2) |
| `X402_UPTO_MAX_AMOUNT` | Max ceiling per run for upto scheme (default: `$0.50`) |
| `SIWE_DOMAIN` | Your public domain (e.g. `api.teardrop.dev`) |
| `CORS_ORIGINS` | Comma-separated allowed origins |
| `AGENT_WALLET_ENABLED` | `true` to enable per-org CDP-backed wallets |
| `CDP_API_KEY_ID` | Coinbase Developer Platform API key ID |
| `CDP_API_KEY_SECRET` | CDP API key secret (Ed25519 / ECDSA) |
| `CDP_WALLET_SECRET` | CDP wallet secret (decrypts TEE-stored keys) |
| `CDP_NETWORK` | CDP network: `base-sepolia` (testnet) or `base` (mainnet) |
| `AGENT_WALLET_MAX_BALANCE_USDC` | Max USDC per agent wallet (default: 100000000 = $100) |
| `MARKETPLACE_ENABLED` | `true` to activate the tool marketplace catalog and platform tool billing in the MCP gateway |
| `MARKETPLACE_SETTLEMENT_CDP_ACCOUNT` | CDP account name for settlement transfers (default: `td-marketplace`) |
| `MARKETPLACE_SETTLEMENT_CHAIN_ID` | Chain for USDC sweeps: `8453` = Base mainnet (production), `84532` = Base Sepolia (testnet). Must match `CDP_NETWORK`. |
| `MARKETPLACE_TX_CONFIRM_TIMEOUT_SECONDS` | Seconds to wait for on-chain tx receipt after CDP transfer (default: `90`). Base mainnet can experience 60–90s delays under congestion. |
| `MARKETPLACE_AUTO_SWEEP_ENABLED` | `true` to auto-sweep org earnings on a schedule |
| `MARKETPLACE_SWEEP_INTERVAL_SECONDS` | Sweep cadence in seconds (default: `86400` = 1 day) |
| `REPUTATION_ROLLUP_ENABLED` | `true` to enable periodic recomputation of reputation metrics from tool call event logs (default: `false`) |
| `REPUTATION_ROLLUP_INTERVAL_SECONDS` | Interval in seconds between reputation rollup passes (default: `3600` = 1 hour) |
| `MARKETPLACE_CATALOG_URL` | Public URL of the marketplace catalog used in tool-deactivation emails (optional) |
| `TOOL_BREAKER_ENABLED` | `true` to auto-deactivate marketplace tools whose webhooks repeatedly fail (default: `true`) |
| `TOOL_BREAKER_THRESHOLD` | Consecutive failures within the window that trip the breaker (default: `5`) |
| `TOOL_BREAKER_WINDOW_SECONDS` | Sliding-window duration in seconds for failure counting (default: `600`) |
| `TOOL_BREAKER_VOLUME_THRESHOLD` | Minimum total tool calls required during the window to trip the breaker. |
| `TOOL_CALL_EVENT_LOGGING_ENABLED` | `true` to persist per-tool-call telemetry (latency, success, error classes, param hashes) for future ML modeling and reputation rolls (default: `true`) |
| `BYOK_TIER_PRICING_ENABLED` | `true` to use per-token orchestration pricing for BYOK orgs (seeded by migration 041). When `false`, uses legacy flat `byok_platform_fee_usdc`. Default: `false` for backward compatibility. |
| `OPENROUTER_API_KEY` | Required if `AGENT_PROVIDER=openrouter` |
| `COINGECKO_API_KEY` | CoinGecko API key for live price data (optional; rate-limited without key) |
| `TAVILY_API_KEY` | Tavily API key for the `web_search` tool (optional; web search disabled without it) |
| `ETHEREUM_RPC_URL` | Ethereum mainnet JSON-RPC URL (required by Ethereum-based tools) |
| `BASE_RPC_URL` | Base L2 JSON-RPC URL (required by Base-based tools and marketplace auto-sweep) |
| `LANGSMITH_TRACING` | Enable LangSmith tracing (default: `false`) |
| `LANGSMITH_API_KEY` | LangSmith API key for tracing |
| `LANGSMITH_PROJECT` | LangSmith project name (default: `teardrop`) |
| `ORG_TOOL_ENCRYPTION_KEY` | Fernet key for encrypting webhook `auth_header_value` at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `LLM_CONFIG_ENCRYPTION_KEY` | Fernet key for encrypting BYOK API keys at rest (same format as above) |
| `REQUIRE_EMAIL_VERIFICATION` | `true` to require email verification before login (default: `false`) |
| `ALLOW_PUBLIC_REGISTRATION` | `false` to disable `POST /register` and force invite-only onboarding (default: `true`) |
| `TURNSTILE_SECRET_KEY` | Cloudflare Turnstile secret key for server-side CAPTCHA verification on `POST /register` (optional) |
| `TURNSTILE_VERIFY_URL` | Turnstile siteverify URL (default: `https://challenges.cloudflare.com/turnstile/v0/siteverify`) |
| `RESEND_API_KEY` | Resend API key for sending verification / invite emails |
| `RESEND_FROM_EMAIL` | Sender address for transactional emails (e.g. `noreply@yourdomain.com`) |
| `APP_BASE_URL` | Public URL of this deployment (used in email links, e.g. `https://api.teardrop.dev`) |
| `MARKETPLACE_DEFAULT_REVENUE_SHARE_BPS` | Author revenue share in basis points (default: `7000` = 70% to author, 30% to platform). Hard-coded split; per-author overrides are not supported. |
| `MCP_AUTH_ENABLED` | `true` to require authentication on the `/tools/mcp` MCP gateway |
| `MCP_AUTH_AUDIENCE` | JWT audience for MCP gateway tokens (default: `teardrop-mcp`) |
| `MCP_BILLING_ENABLED` | `true` to enable credit billing for MCP tool calls |
| `MCP_X402_ENABLED` | `true` to accept x402 payments on the MCP gateway |
| `MEMORY_ENABLED` | Enable persistent agent memory (default: `true`). Auto-disabled if `OPENAI_API_KEY` is unset. |
| `SENTRY_DSN` | Sentry error tracking DSN (optional; leave empty to disable) |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Refresh token validity window in days (default: `30`) |
| `RATE_LIMIT_AUTH_RPM` | Per-IP rate limit for `/token` and `/auth/siwe/nonce` (default: `20`) |
| `RATE_LIMIT_REGISTER_RPM` | Per-IP rate limit for `POST /register` (default: `5`) |
| `AUTH_LOCKOUT_THRESHOLD` | Failed email-login attempts before temporary lockout (default: `10`) |
| `AUTH_LOCKOUT_WINDOW_SECONDS` | Failed email-login lockout window in seconds (default: `900`) |
| `RATE_LIMIT_AGENT_RPM` | Per-user rate limit for `/agent/run` (default: `30`) |
| `RATE_LIMIT_ORG_AGENT_RPM` | Per-org aggregate rate limit for `/agent/run` (default: `100`) |
| `RATE_LIMIT_ORG_MCP_RPM` | Per-org rate limit for MCP gateway (default: `200`) |
| `RATE_LIMIT_WEBHOOK_RPM` | Per-IP rate limit for Stripe webhook (default: `120`) |
| `RATE_LIMIT_TEST_WEBHOOK_RPM` | Per-org rate limit for test-webhook endpoint (default: `10`) |
| `TRUSTED_PROXY_COUNT` | Trusted proxy hops when deriving client IP from `X-Forwarded-For` (default: `1`; set `0` to ignore the header) |
