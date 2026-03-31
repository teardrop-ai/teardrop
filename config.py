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

    # ── Checkpointing ──────────────────────────────────────────────────────────
    checkpoint_db_path: str = Field(
        default="data/teardrop.db", description="SQLite database path for LangGraph checkpointing"
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
