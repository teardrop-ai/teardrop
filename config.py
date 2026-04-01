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
    cors_origins: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        if self.cors_origins == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ── LLM / Anthropic ────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    agent_model: str = "claude-3-5-sonnet-20241022"
    agent_max_tokens: int = 4096
    agent_temperature: float = 0.0

    # ── LangSmith ──────────────────────────────────────────────────────────────
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "teardrop"

    # ── Tool Providers ──────────────────────────────────────────────────────────
    tavily_api_key: str = Field(default="", description="Tavily API key for web search")

    # ── Tool Registry ──────────────────────────────────────────────────────────
    tool_deprecation_window_days: int = Field(
        default=90, description="Days before a deprecated tool version is removed"
    )

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    rate_limit_requests_per_minute: int = 60

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
    jwt_client_id: str = Field(default="teardrop-client", description="Client ID for token endpoint")
    jwt_client_secret: str = Field(default="", description="Client secret for token endpoint (set in .env)")

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
    base_rpc_url: str = Field(
        default="", description="Base L2 JSON-RPC URL"
    )
    siwe_domain: str = Field(
        default="", description="Expected domain in SIWE messages (defaults to app_host if empty)"
    )
    siwe_nonce_ttl_seconds: int = Field(
        default=300, description="SIWE nonce validity window in seconds"
    )

    @property
    def effective_siwe_domain(self) -> str:
        return self.siwe_domain or self.app_host

    # ── Checkpointing ──────────────────────────────────────────────────────────
    checkpoint_db_path: str = Field(
        default="data/teardrop.db", description="SQLite database path for LangGraph checkpointing"
    )

    # ── User / Usage Database ──────────────────────────────────────────────────
    user_db_path: str = Field(
        default="data/teardrop.db", description="SQLite database path for users, orgs, and usage events"
    )

    # ── Postgres (Neon) ────────────────────────────────────────────────────────
    database_url: str = Field(
        default="",
        description="Postgres connection string (postgresql://...). Required for production.",
    )

    @property
    def pg_dsn(self) -> str:
        """Standard Postgres DSN, stripping any SQLAlchemy dialect prefix."""
        return self.database_url.replace("+asyncpg", "")

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
        default="$0.01", description="Flat price per /agent/run request (exact scheme, used as fallback)"
    )
    x402_scheme: str = Field(
        default="exact",
        description="Payment scheme: 'exact' (flat per-run) or 'upto' (usage-based, requires x402 upto support)",
    )
    pricing_cache_ttl_seconds: int = Field(
        default=300,
        description="How long to cache the active pricing_rules row before re-querying (seconds)",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
