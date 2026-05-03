# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from functools import cached_property, lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────────
    app_env: Literal["development", "production", "test"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    # ── Observability (Sentry) ────────────────────────────────────────────────
    # Empty DSN disables Sentry entirely (no SDK init, no network). The DSN is
    # a write-only ingest key (not a secret) but is kept in env for cleanliness.
    sentry_dsn: str = Field(default="", description="Sentry DSN; empty = disabled")
    sentry_environment: str = Field(
        default="",
        description="Override Sentry environment label; defaults to app_env when empty",
    )

    # ── CORS ───────────────────────────────────────────────────────────────────
    # Empty string or "*" both mean "allow all origins" — acceptable for fully
    # public APIs using bearer-token auth, but should be restricted to your
    # frontend domain in production for defense-in-depth.
    cors_origins: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        if self.cors_origins in ("", "*"):
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ── LLM / Anthropic ────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    openai_api_key: str = Field(default="", description="OpenAI API key")
    google_api_key: str = Field(default="", description="Google AI API key")
    openrouter_api_key: str = Field(default="", description="OpenRouter API key")

    agent_provider: str = Field(
        default="openrouter",
        description="LLM provider: anthropic, openai, google, or openrouter",
    )
    agent_model: str = "deepseek/deepseek-v4-flash"
    agent_max_tokens: int = 4096
    agent_temperature: float = 0.0
    agent_llm_timeout_seconds: int = Field(default=180, description="Timeout in seconds for the planner LLM call")
    agent_synthesis_max_tokens: int = Field(
        default=2048,
        description=(
            "Maximum output tokens for planner synthesis turns after at least one "
            "tool iteration. Keeps final synthesis concise and reduces timeout risk."
        ),
    )
    agent_synthesis_provider: str = Field(
        default="",
        description=(
            "Optional override provider for synthesis-only planner turns "
            "(tool_iterations >= 1). When unset, the primary agent_provider is used. "
            "Use this to route long final-synthesis calls through a faster model "
            "(e.g., openai/gpt-4o-mini) without changing the planner model."
        ),
    )
    agent_synthesis_model: str = Field(
        default="",
        description=(
            "Optional override model for synthesis-only planner turns. "
            "Only applied when agent_synthesis_provider is also set and the "
            "request has no org-level BYOK config."
        ),
    )
    agent_ui_generator_timeout_seconds: int = Field(default=60, description="Timeout in seconds for the UI generator LLM call")
    agent_max_tool_iterations: int = Field(
        default=4,
        description=(
            "Maximum planner→tool cycles per agent run. "
            "When exceeded the agent synthesises from partial data rather than looping further."
        ),
    )
    agent_tool_max_calls_per_run: dict[str, int] = Field(
        default_factory=lambda: {"get_yield_rates": 1, "resolve_ens": 1},
        description=(
            "Per-run hard caps for specific platform tools keyed by tool name. "
            "Values are max allowed calls regardless of argument variation."
        ),
    )
    agent_tool_executor_timeout_seconds: int = Field(
        default=120,
        description=(
            "Timeout in seconds for tool_executor_node. Aborts all tool execution if any tool hangs. "
            "Prevents runaway RPC calls or webhook timeouts from blocking the agent."
        ),
    )
    agent_single_tool_timeout_seconds: int = Field(
        default=30,
        description=(
            "Timeout in seconds for a single tool call within a batch. "
            "Timed-out tools are converted into non-billable error ToolMessages so "
            "the planner can synthesize from partial data."
        ),
    )
    agent_rpc_call_timeout_seconds: int = Field(
        default=15,
        description=(
            "Timeout in seconds for individual RPC calls (eth_call, eth_getBalance, etc.). "
            "Prevents hanging on single slow RPC requests from public providers."
        ),
    )
    agent_rpc_semaphore_limit: int = Field(
        default=8,
        description=(
            "Global limit on concurrent RPC calls across all agent runs. "
            "Prevents organizational RPC saturation. Typical public provider limit: 5–10 concurrent calls; "
            "default is tuned to stay within that range on shared RPC nodes."
        ),
    )
    agent_rpc_chain_semaphore_limit: int = Field(
        default=2,
        description=(
            "Per-chain limit on concurrent RPC calls. Applied in addition to the global semaphore "
            "to prevent same-chain bursts from monopolizing RPC capacity."
        ),
    )
    agent_rpc_chain_rps_limit: int = Field(
        default=3,
        description=(
            "Per-chain token-bucket refill rate (requests per second). Applied alongside "
            "RPC semaphores to smooth burst traffic and reduce upstream 429 responses."
        ),
    )
    agent_max_tool_result_chars: int = Field(
        default=8000,
        description=(
            "Maximum number of characters from any single tool result retained in conversation "
            "state before truncation to keep planner context bounded."
        ),
    )
    agent_ui_generator_provider: str = Field(
        default="google",
        description="Provider for UI generation turns when no org-level BYOK config is set.",
    )
    agent_ui_generator_model: str = Field(
        default="gemini-3-flash-preview",
        description="Model for UI generation turns when no org-level BYOK config is set.",
    )

    # ── LangSmith ──────────────────────────────────────────────────────────────
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "teardrop"

    # ── Tool Providers ──────────────────────────────────────────────────────────
    tavily_api_key: str = Field(default="", description="Tavily API key for web search")
    coingecko_api_key: str = Field(default="", description="CoinGecko demo API key (optional, raises rate limits)")
    coingecko_api_url: str = Field(
        default="https://api.coingecko.com/api/v3",
        description="CoinGecko API base URL",
    )

    # ── Tool Registry ──────────────────────────────────────────────────────────
    tool_deprecation_window_days: int = Field(default=90, description="Days before a deprecated tool version is removed")

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    rate_limit_requests_per_minute: int = 60
    rate_limit_agent_rpm: int = Field(default=30, description="Per-user rate limit for /agent/run (requests per minute)")
    rate_limit_auth_rpm: int = Field(
        default=20,
        description="Per-IP rate limit for /token and /auth/siwe/nonce (requests per minute)",
    )
    rate_limit_register_rpm: int = Field(
        default=5,
        description="Per-IP rate limit for POST /register (requests per minute). Intentionally lower than auth.",
    )
    rate_limit_org_agent_rpm: int = Field(
        default=100,
        description=(
            "Per-org aggregate rate limit for /agent/run (requests per minute). "
            "Applies across all users in the org. Prevents noisy-neighbor saturation."
        ),
    )
    rate_limit_org_mcp_rpm: int = Field(
        default=200,
        description=("Per-org aggregate rate limit for MCP requests via the gateway (requests per minute)."),
    )
    rate_limit_webhook_rpm: int = Field(
        default=120,
        description="Per-IP rate limit for POST /billing/topup/webhook (requests per minute).",
    )
    rate_limit_test_webhook_rpm: int = Field(
        default=10,
        description="Per-org rate limit for POST /tools/test-webhook (requests per minute).",
    )

    # ── JWT Authentication ─────────────────────────────────────────────────────
    jwt_private_key_path: str = Field(
        default="keys/private.pem", description="Path to RSA private key (relative to project root)"
    )
    jwt_public_key_path: str = Field(default="keys/public.pem", description="Path to RSA public key (relative to project root)")
    jwt_algorithm: str = "RS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_issuer: str = "teardrop"
    jwt_client_id: str = Field(default="teardrop-client", description="Client ID for token endpoint")
    jwt_client_secret: str = Field(default="", description="Client secret for token endpoint (set in .env)")

    @cached_property
    def jwt_private_key(self) -> str:
        return (_PROJECT_ROOT / self.jwt_private_key_path).read_text()

    @cached_property
    def jwt_public_key(self) -> str:
        return (_PROJECT_ROOT / self.jwt_public_key_path).read_text()

    # ── Web3 / SIWE ────────────────────────────────────────────────────────────
    ethereum_rpc_url: str = Field(default="", description="Ethereum mainnet JSON-RPC URL (Alchemy/Infura/etc.)")
    base_rpc_url: str = Field(default="", description="Base L2 JSON-RPC URL")
    siwe_domain: str = Field(default="", description="Expected domain in SIWE messages (defaults to app_host if empty)")
    siwe_nonce_ttl_seconds: int = Field(default=300, description="SIWE nonce validity window in seconds")

    @property
    def effective_siwe_domain(self) -> str:
        return self.siwe_domain or self.app_host

    # ── Postgres (Neon) ────────────────────────────────────────────────────────
    database_url: str = Field(
        default="",
        description="Postgres connection string (postgresql://...). Required for production.",
    )

    @property
    def pg_dsn(self) -> str:
        """Standard Postgres DSN, stripping any SQLAlchemy dialect prefix."""
        return self.database_url.replace("+asyncpg", "")

    # ── Redis (optional) ──────────────────────────────────────────────────────────
    redis_url: str = Field(
        default="",
        description=(
            "Redis connection URL (optional, e.g. redis://localhost:6379). "
            "Required for multi-container deployments to share rate limiting, SIWE nonces, and "
            "pricing cache. Leave unset for single-container or local development."
        ),
    )

    # ── Billing / x402 ────────────────────────────────────────────────────────
    billing_enabled: bool = Field(default=False, description="Enable x402 on-chain billing for paid endpoints")
    x402_facilitator_url: str = Field(
        default="https://x402.org/facilitator",
        description="x402 facilitator URL (testnet default; use Coinbase for mainnet)",
    )
    x402_pay_to_address: str = Field(default="", description="Treasury wallet address that receives USDC payments")
    x402_network: str = Field(
        default="eip155:84532",
        description="x402 network identifier (Base Sepolia for dev, eip155:8453 for prod)",
    )
    x402_run_price: str = Field(
        default="$0.01",
        description="Flat price per /agent/run request (exact scheme, used as fallback)",
    )
    x402_scheme: str = Field(
        default="exact",
        description=("Payment scheme: 'exact' (flat per-run) or 'upto' (usage-based, requires x402 upto support)"),
    )
    x402_upto_max_amount: str = Field(
        default="$0.50",
        description=(
            "Maximum per-run amount advertised in upto scheme 402 response. "
            "Client signs this as the ceiling; actual settlement is based on usage."
        ),
    )

    @property
    def x402_upto_max_amount_atomic(self) -> int:
        """Parse `x402_upto_max_amount` (e.g. "$0.50") into atomic USDC (6-decimal int).

        Returns 0 if the string cannot be parsed.
        """
        s = self.x402_upto_max_amount.strip().lstrip("$").strip()
        try:
            return int(round(float(s) * 1_000_000))
        except (ValueError, TypeError):
            return 0

    pricing_cache_ttl_seconds: int = Field(
        default=300,
        description="How long to cache the active pricing_rules row before re-querying (seconds)",
    )
    byok_platform_fee_usdc: int = Field(
        default=1000,
        description=(
            "Minimum flat fee in atomic USDC charged per /agent/run for BYOK orgs. "
            "1000 = $0.001. Acts as a floor when byok_tier_pricing_enabled=True "
            "(computed token-based fee is max(computed, floor)). "
            "Also used as the legacy flat fee when byok_tier_pricing_enabled=False. "
            "Set to 0 to disable any minimum floor."
        ),
    )
    byok_tier_pricing_enabled: bool = Field(
        default=False,
        description=(
            "When True, BYOK orgs are billed a per-token orchestration fee resolved "
            "from pricing_rules (is_byok=True rows, seeded by migration 041) rather "
            "than the flat byok_platform_fee_usdc. The flat fee becomes a floor. "
            "Enable after verifying migration 041 has been applied."
        ),
    )
    # ── Stripe (prepaid credit top-up) ────────────────────────────────────────
    stripe_secret_key: str = Field(default="", description="Stripe secret key (sk_live_... or sk_test_...)")
    stripe_webhook_secret: str = Field(default="", description="Stripe webhook signing secret (whsec_...)")

    # ── Database ──────────────────────────────────────────────────────────────
    pg_command_timeout: float = Field(
        default=30.0,
        description="Default asyncpg command timeout in seconds for all DB queries.",
    )

    # Which auth methods are subject to billing.  SIWE callers pay via x402
    # payment headers; other listed methods are checked against the org's
    # prepaid credit balance instead.  Default: SIWE-only (existing behaviour).
    billable_auth_methods: list[str] = Field(
        default=["siwe", "client_credentials", "email"],
        description=(
            "Auth methods that require payment. 'siwe' uses x402 on-chain; "
            "'client_credentials' and 'email' use the org prepaid credit ledger instead."
        ),
    )

    # ── Settlement retry ─────────────────────────────────────────────────────
    settlement_retry_interval_seconds: int = Field(
        default=10, description="Background worker poll interval for retrying failed settlements"
    )
    settlement_max_retries: int = Field(default=5, description="Max retry attempts before marking a settlement as exhausted")
    x402_settlement_timeout_seconds: int = Field(
        default=30,
        description=(
            "Hard timeout for the in-stream x402 settle_payment() call. On timeout the "
            "settlement is enqueued via enqueue_failed_settlement() and retried by the "
            "background worker, so the SSE stream is never held hostage by a slow facilitator."
        ),
    )
    agent_state_snapshot_timeout_seconds: float = Field(
        default=10.0,
        description=(
            "Hard timeout for graph.aget_state() after astream_events completes. On timeout "
            "usage_data falls back to {} and the stream proceeds to emit DONE."
        ),
    )

    # ── Persistent Memory (per-org RAG) ─────────────────────────────────────
    memory_enabled: bool = Field(
        default=True,
        description="Enable persistent agent memory. Auto-disabled if openai_api_key is empty.",
    )
    memory_top_k: int = Field(default=5, description="Number of memories to retrieve per agent run")
    memory_max_per_org: int = Field(default=1000, description="Maximum stored memories per organisation")
    memory_ttl_days: int = Field(default=0, description="Memory expiry in days (0 = never expire)")
    memory_cleanup_interval_seconds: int = Field(
        default=3600, description="Background worker interval for deleting expired memories"
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model for memory vectors",
    )
    embedding_dimensions: int = Field(
        default=1536,
        description="Embedding vector dimensions (must match VECTOR(N) in DB schema)",
    )

    # ── Custom Tools (per-org webhook tools) ──────────────────────────────────
    max_org_tools: int = Field(default=50, description="Maximum custom tools per organisation")
    max_custom_tool_calls_per_run: int = Field(default=5, description="Maximum custom-tool webhook calls per agent run")
    org_tool_encryption_key: str = Field(
        default="",
        description=("Fernet key for encrypting webhook auth headers (generate via Fernet.generate_key())"),
    )
    org_tools_cache_ttl_seconds: int = Field(default=60, description="TTL for per-org tool cache in seconds")

    # ── Tool Health / Circuit Breaker ────────────────────────────────────────
    tool_breaker_enabled: bool = Field(
        default=True,
        description="Enable Redis-backed circuit breaker for failing webhook tools.",
    )
    tool_breaker_threshold: int = Field(
        default=5,
        ge=1,
        description="Consecutive webhook failures within the window before auto-deactivation.",
    )
    tool_breaker_window_seconds: int = Field(
        default=600,
        ge=60,
        description="Sliding window for the failure counter (seconds).",
    )

    # ── LLM Config Encryption ─────────────────────────────────────────────────
    llm_config_encryption_key: str = Field(
        default="",
        description=(
            "Separate Fernet key for encrypting BYOK LLM API keys at rest. "
            "Falls back to org_tool_encryption_key if empty. "
            "Generate via: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        ),
    )

    # ── MCP Client (per-org external MCP servers) ─────────────────────────────
    max_org_mcp_servers: int = Field(default=5, description="Maximum external MCP servers per organisation")
    max_mcp_tools_per_server: int = Field(default=50, description="Maximum tools to import from a single MCP server")
    mcp_client_connect_timeout_seconds: int = Field(
        default=10, description="Timeout for establishing a connection to an MCP server"
    )
    mcp_client_tool_cache_ttl_seconds: int = Field(
        default=300, description="TTL for caching discovered MCP tool definitions (seconds)"
    )

    # ── A2A Delegation (outbound agent-to-agent calls) ────────────────────────
    a2a_delegation_enabled: bool = Field(default=False, description="Enable outbound A2A delegation via delegate_to_agent tool")
    a2a_delegation_timeout_seconds: int = Field(
        default=120, description="HTTP timeout for outbound A2A /message:send calls (seconds)"
    )
    a2a_delegation_max_per_run: int = Field(default=3, description="Maximum delegate_to_agent calls allowed per agent run")
    a2a_delegation_require_allowlist: bool = Field(
        default=True,
        description="When true, delegation fails if the target agent is not on the org's allowlist. Security-first default.",
    )
    a2a_agent_card_cache_ttl_seconds: int = Field(default=300, description="TTL for caching remote agent cards (seconds)")

    # ── A2A Delegation Billing ────────────────────────────────────────────────
    a2a_delegation_billing_enabled: bool = Field(
        default=False,
        description="Enable billing for outbound A2A delegations (fund from org credits or x402)",
    )
    a2a_delegation_platform_fee_bps: int = Field(
        default=500,
        description="Platform fee on delegations in basis points (500 = 5%%)",
    )
    a2a_delegation_max_cost_usdc: int = Field(
        default=100_000,
        description="Global per-delegation cost cap in atomic USDC (default $0.10)",
    )
    x402_treasury_private_key: str = Field(
        default="",
        description="Hex-encoded private key for the platform treasury wallet (signs outbound x402 payments)",
    )

    # ── CDP Agent Wallets (per-org managed wallets via Coinbase Developer Platform) ─
    agent_wallet_enabled: bool = Field(
        default=False,
        description="Enable CDP-backed agent wallets (per-org USDC wallets for A2A payments)",
    )
    cdp_api_key_id: str = Field(
        default="",
        description="Coinbase Developer Platform API key ID",
    )
    cdp_api_key_secret: str = Field(
        default="",
        description="Coinbase Developer Platform API key secret (Ed25519 / ECDSA)",
    )
    cdp_wallet_secret: str = Field(
        default="",
        description="Coinbase Developer Platform wallet secret (decrypts TEE-stored keys)",
    )
    cdp_network: str = Field(
        default="base-sepolia",
        description="CDP network name: 'base-sepolia' (testnet) or 'base' (mainnet)",
    )
    agent_wallet_max_balance_usdc: int = Field(
        default=100_000_000,
        description="Maximum USDC balance per agent wallet in atomic units (100_000_000 = $100)",
    )

    @property
    def cdp_configured(self) -> bool:
        """True when all three CDP secrets are set."""
        return bool(self.cdp_api_key_id and self.cdp_api_key_secret and self.cdp_wallet_secret)

    # ── Multi-LLM Gateway ─────────────────────────────────────────────────────
    allow_private_llm_endpoints: bool = Field(
        default=False,
        description="Allow LLM api_base URLs pointing to private IPs (for self-hosted inference)",
    )
    default_model_pool: list[dict[str, str]] = Field(
        default=[
            # Cost tier — DeepSeek V4 Flash via OpenRouter (US providers: NovitaAI primary, DeepInfra fallback).
            {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"},
            # Speed tier — Gemini 3 Flash (1M context, sub-400ms median).
            {"provider": "google", "model": "gemini-3-flash-preview"},
            # Quality tier — Claude Sonnet 4.6 (200k context, top-tier reasoning).
            {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        ],
        description="Models available for smart routing (Teardrop holds shared keys)",
    )

    # ── Marketplace (paid MCP tool hosting + author revenue share) ─────────────
    marketplace_enabled: bool = Field(default=False, description="Enable the paid MCP tool marketplace")
    marketplace_catalog_url: str = Field(
        default="",
        description="Public URL of the marketplace catalog (used in subscriber notification emails).",
    )
    marketplace_default_revenue_share_bps: int = Field(
        default=7000,
        description="Default author revenue share in basis points (7000 = 70%)",
    )
    marketplace_minimum_withdrawal_usdc: int = Field(
        default=100_000,
        description="Minimum withdrawal amount in atomic USDC (100000 = $0.10)",
    )
    marketplace_withdrawal_cooldown_seconds: int = Field(
        default=3600,
        description="Minimum seconds between withdrawal requests per org",
    )
    marketplace_settlement_cdp_account: str = Field(
        default="td-marketplace",
        description="CDP account name for the marketplace settlement pool wallet",
    )
    marketplace_settlement_chain_id: int = Field(
        default=84532,
        description="EIP-155 chain ID for marketplace settlements (84532=Base Sepolia, 8453=Base)",
    )
    marketplace_auto_sweep_enabled: bool = Field(
        default=False,
        description="Enable background task to auto-process withdrawals for qualifying orgs",
    )
    marketplace_sweep_interval_seconds: int = Field(
        default=86400,
        description="Interval in seconds between marketplace auto-sweep runs (default: 24h)",
    )
    marketplace_max_sweep_retries: int = Field(
        default=5,
        description=(
            "Max sweep attempts per withdrawal before marking it 'exhausted'. "
            "Backoff: min(2^attempt * 60s, 86400s). Matches settlement_max_retries default."
        ),
    )
    marketplace_tx_confirm_timeout_seconds: int = Field(
        default=90,
        description=(
            "Seconds to wait for an on-chain transaction receipt after CDP transfer. "
            "Base L2 blocks normally in ~2s, but under network congestion mainnet txs "
            "can sit in the mempool for 60–90s. 90s provides a comfortable margin for "
            "production (45 polling attempts at 2s intervals) while keeping withdrawal "
            "latency reasonable."
        ),
    )
    marketplace_settlement_warn_threshold_usdc: int = Field(
        default=5_000_000,
        description=(
            "Log an ERROR after each sweep cycle when the settlement wallet USDC "
            "balance falls below this threshold (atomic units; 5_000_000 = $5.00)."
        ),
    )
    rate_limit_mcp_rpm: int = Field(
        default=30,
        description="Per-user rate limit for MCP marketplace tool calls (requests per minute)",
    )

    # ── MCP Gateway (auth / billing / x402) ───────────────────────────────────
    mcp_auth_enabled: bool = Field(
        default=False,
        description="Require a valid Teardrop JWT to access /tools/mcp (Phase 1)",
    )
    mcp_auth_audience: str = Field(
        default="teardrop-mcp",
        description="Expected JWT 'aud' claim for MCP tokens (security: prevents tokens from other apps)",
    )
    mcp_billing_enabled: bool = Field(
        default=False,
        description="Debit org credits for each MCP tools/call request (Phase 2)",
    )
    mcp_x402_enabled: bool = Field(
        default=False,
        description="Accept x402 on-chain payment for anonymous MCP callers (Phase 3)",
    )

    # ── Email / Resend ────────────────────────────────────────────────────────
    require_email_verification: bool = Field(
        default=False,
        description=(
            "When True, email-based login is blocked until the user verifies their email. "
            "Safe to enable after marketing push — admin-created users default to verified."
        ),
    )
    resend_api_key: str = Field(default="", description="Resend API key for transactional email")
    resend_from_email: str = Field(
        default="noreply@teardrop.dev",
        description="Sender address for verification and invite emails",
    )
    app_base_url: str = Field(
        default="",
        description="Public base URL (e.g. https://teardrop.dev) used to build email links",
    )

    # ── Refresh Tokens ────────────────────────────────────────────────────────
    refresh_token_expire_days: int = Field(default=30, description="Refresh token validity window in days")
    refresh_token_cleanup_interval_seconds: int = Field(
        default=3600,
        description="Background worker interval for deleting revoked+expired refresh tokens (seconds)",
    )
    refresh_token_idempotency_window_seconds: int = Field(
        default=60,
        description=(
            "How long after rotation a client may replay the old refresh token and receive "
            "the successor tokens. Handles network timeouts where the 200 was never delivered."
        ),
    )

    # ── Validators ─────────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_model_pool(self) -> "Settings":
        """Ensure every entry in default_model_pool has a valid provider."""
        from agent.llm import ALLOWED_PROVIDERS

        for entry in self.default_model_pool:
            provider = entry.get("provider", "")
            if provider not in ALLOWED_PROVIDERS:
                raise ValueError(
                    f"default_model_pool contains unknown provider '{provider}'. Allowed: {', '.join(sorted(ALLOWED_PROVIDERS))}"
                )
            if not entry.get("model"):
                raise ValueError("default_model_pool entry missing 'model' key")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
