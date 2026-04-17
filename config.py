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
    agent_provider: str = Field(
        default="anthropic",
        description="LLM provider: anthropic, openai, or google",
    )
    agent_model: str = "claude-haiku-4-5-20251001"
    agent_max_tokens: int = 4096
    agent_temperature: float = 0.0
    agent_llm_timeout_seconds: int = Field(
        default=120, description="Timeout in seconds for the planner LLM call"
    )
    agent_ui_generator_timeout_seconds: int = Field(
        default=60, description="Timeout in seconds for the UI generator LLM call"
    )

    # ── LangSmith ──────────────────────────────────────────────────────────────
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "teardrop"

    # ── Tool Providers ──────────────────────────────────────────────────────────
    tavily_api_key: str = Field(default="", description="Tavily API key for web search")
    coingecko_api_key: str = Field(
        default="", description="CoinGecko demo API key (optional, raises rate limits)"
    )
    coingecko_api_url: str = Field(
        default="https://api.coingecko.com/api/v3",
        description="CoinGecko API base URL",
    )

    # ── Tool Registry ──────────────────────────────────────────────────────────
    tool_deprecation_window_days: int = Field(
        default=90, description="Days before a deprecated tool version is removed"
    )

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    rate_limit_requests_per_minute: int = 60
    rate_limit_agent_rpm: int = Field(
        default=30, description="Per-user rate limit for /agent/run (requests per minute)"
    )
    rate_limit_auth_rpm: int = Field(
        default=20,
        description="Per-IP rate limit for /token and /auth/siwe/nonce (requests per minute)",
    )

    # ── JWT Authentication ─────────────────────────────────────────────────────
    jwt_private_key_path: str = Field(
        default="keys/private.pem", description="Path to RSA private key (relative to project root)"
    )
    jwt_public_key_path: str = Field(
        default="keys/public.pem", description="Path to RSA public key (relative to project root)"
    )
    jwt_algorithm: str = "RS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_issuer: str = "teardrop"
    jwt_client_id: str = Field(
        default="teardrop-client", description="Client ID for token endpoint"
    )
    jwt_client_secret: str = Field(
        default="", description="Client secret for token endpoint (set in .env)"
    )

    @cached_property
    def jwt_private_key(self) -> str:
        return (_PROJECT_ROOT / self.jwt_private_key_path).read_text()

    @cached_property
    def jwt_public_key(self) -> str:
        return (_PROJECT_ROOT / self.jwt_public_key_path).read_text()

    # ── Web3 / SIWE ────────────────────────────────────────────────────────────
    ethereum_rpc_url: str = Field(
        default="", description="Ethereum mainnet JSON-RPC URL (Alchemy/Infura/etc.)"
    )
    base_rpc_url: str = Field(default="", description="Base L2 JSON-RPC URL")
    siwe_domain: str = Field(
        default="", description="Expected domain in SIWE messages (defaults to app_host if empty)"
    )
    siwe_nonce_ttl_seconds: int = Field(
        default=300, description="SIWE nonce validity window in seconds"
    )

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
    billing_enabled: bool = Field(
        default=False, description="Enable x402 on-chain billing for paid endpoints"
    )
    x402_facilitator_url: str = Field(
        default="https://x402.org/facilitator",
        description="x402 facilitator URL (testnet default; use Coinbase for mainnet)",
    )
    x402_pay_to_address: str = Field(
        default="", description="Treasury wallet address that receives USDC payments"
    )
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
        description=(
            "Payment scheme: 'exact' (flat per-run) or 'upto' (usage-based, requires x402 "
            "upto support)"
        ),
    )
    x402_upto_max_amount: str = Field(
        default="$0.50",
        description=(
            "Maximum per-run amount advertised in upto scheme 402 response. "
            "Client signs this as the ceiling; actual settlement is based on usage."
        ),
    )
    pricing_cache_ttl_seconds: int = Field(
        default=300,
        description="How long to cache the active pricing_rules row before re-querying (seconds)",
    )
    # ── Stripe (prepaid credit top-up) ────────────────────────────────────────
    stripe_secret_key: str = Field(
        default="", description="Stripe secret key (sk_live_... or sk_test_...)"
    )
    stripe_webhook_secret: str = Field(
        default="", description="Stripe webhook signing secret (whsec_...)"
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
    settlement_max_retries: int = Field(
        default=5, description="Max retry attempts before marking a settlement as exhausted"
    )

    # ── Persistent Memory (per-org RAG) ─────────────────────────────────────
    memory_enabled: bool = Field(
        default=True,
        description="Enable persistent agent memory. Auto-disabled if openai_api_key is empty.",
    )
    memory_top_k: int = Field(
        default=5, description="Number of memories to retrieve per agent run"
    )
    memory_max_per_org: int = Field(
        default=1000, description="Maximum stored memories per organisation"
    )
    memory_ttl_days: int = Field(
        default=0, description="Memory expiry in days (0 = never expire)"
    )
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
    max_org_tools: int = Field(
        default=50, description="Maximum custom tools per organisation"
    )
    max_custom_tool_calls_per_run: int = Field(
        default=5, description="Maximum custom-tool webhook calls per agent run"
    )
    org_tool_encryption_key: str = Field(
        default="",
        description=(
            "Fernet key for encrypting webhook auth headers"
            " (generate via Fernet.generate_key())"
        ),
    )
    org_tools_cache_ttl_seconds: int = Field(
        default=60, description="TTL for per-org tool cache in seconds"
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
    max_org_mcp_servers: int = Field(
        default=5, description="Maximum external MCP servers per organisation"
    )
    max_mcp_tools_per_server: int = Field(
        default=50, description="Maximum tools to import from a single MCP server"
    )
    mcp_client_connect_timeout_seconds: int = Field(
        default=10, description="Timeout for establishing a connection to an MCP server"
    )
    mcp_client_tool_cache_ttl_seconds: int = Field(
        default=300, description="TTL for caching discovered MCP tool definitions (seconds)"
    )

    # ── A2A Delegation (outbound agent-to-agent calls) ────────────────────────
    a2a_delegation_enabled: bool = Field(
        default=False, description="Enable outbound A2A delegation via delegate_to_agent tool"
    )
    a2a_delegation_timeout_seconds: int = Field(
        default=120, description="HTTP timeout for outbound A2A /message:send calls (seconds)"
    )
    a2a_delegation_max_per_run: int = Field(
        default=3, description="Maximum delegate_to_agent calls allowed per agent run"
    )
    a2a_delegation_require_allowlist: bool = Field(
        default=False,
        description="When true, delegation fails if the target agent is not on the org's allowlist",
    )
    a2a_agent_card_cache_ttl_seconds: int = Field(
        default=300, description="TTL for caching remote agent cards (seconds)"
    )

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
            {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "google", "model": "gemini-2.0-flash"},
        ],
        description="Models available for smart routing (Teardrop holds shared keys)",
    )

    # ── Marketplace (paid MCP tool hosting + author revenue share) ─────────────
    marketplace_enabled: bool = Field(
        default=False, description="Enable the paid MCP tool marketplace"
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
    rate_limit_mcp_rpm: int = Field(
        default=30,
        description="Per-user rate limit for MCP marketplace tool calls (requests per minute)",
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
        default="hello@teardrop.dev",
        description="Sender address for verification and invite emails",
    )
    app_base_url: str = Field(
        default="",
        description="Public base URL (e.g. https://teardrop.dev) used to build email links",
    )

    # ── Refresh Tokens ────────────────────────────────────────────────────────
    refresh_token_expire_days: int = Field(
        default=30, description="Refresh token validity window in days"
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
                    f"default_model_pool contains unknown provider '{provider}'. "
                    f"Allowed: {', '.join(sorted(ALLOWED_PROVIDERS))}"
                )
            if not entry.get("model"):
                raise ValueError("default_model_pool entry missing 'model' key")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
