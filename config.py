# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from functools import cached_property, lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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
    a2a_agent_card_cache_ttl_seconds: int = Field(
        default=300, description="TTL for caching remote agent cards (seconds)"
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
