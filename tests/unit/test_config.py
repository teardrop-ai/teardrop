"""Unit tests for config.py — settings loading and derived properties."""

from __future__ import annotations

import config
from config import Settings


def test_default_env_is_development(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    config.get_settings.cache_clear()
    s = Settings()
    assert s.app_env == "development"
    config.get_settings.cache_clear()


def test_pg_dsn_strips_asyncpg_prefix(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@host/db")
    s = Settings()
    assert s.pg_dsn == "postgresql://user:pass@host/db"


def test_pg_dsn_passthrough_plain(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    s = Settings()
    assert s.pg_dsn == "postgresql://user:pass@host/db"


def test_effective_siwe_domain_uses_siwe_domain(monkeypatch):
    monkeypatch.setenv("SIWE_DOMAIN", "example.com")
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    s = Settings()
    assert s.effective_siwe_domain == "example.com"


def test_effective_siwe_domain_falls_back_to_app_host(monkeypatch):
    monkeypatch.setenv("SIWE_DOMAIN", "")
    monkeypatch.setenv("APP_HOST", "myapp.example.com")
    s = Settings()
    assert s.effective_siwe_domain == "myapp.example.com"


def test_cors_origins_wildcard():
    s = Settings(cors_origins="*")
    assert s.cors_origins_list == ["*"]


def test_cors_origins_list_parsing():
    s = Settings(cors_origins="https://a.com, https://b.com")
    assert s.cors_origins_list == ["https://a.com", "https://b.com"]


def test_get_settings_is_cached():
    config.get_settings.cache_clear()
    s1 = config.get_settings()
    s2 = config.get_settings()
    assert s1 is s2
    config.get_settings.cache_clear()


def test_test_settings_fixture_uses_test_env(test_settings):
    assert test_settings.app_env == "test"


def test_jwt_key_paths_readable(test_settings):
    # cached_property reads the actual files — should not raise
    priv = test_settings.jwt_private_key
    pub = test_settings.jwt_public_key
    assert "BEGIN PRIVATE KEY" in priv
    assert "BEGIN PUBLIC KEY" in pub


def test_x402_upto_max_amount_atomic_default():
    s = Settings(x402_upto_max_amount="$0.50")
    assert s.x402_upto_max_amount_atomic == 500_000


def test_x402_upto_max_amount_atomic_one_dollar():
    s = Settings(x402_upto_max_amount="$1.00")
    assert s.x402_upto_max_amount_atomic == 1_000_000


def test_x402_upto_max_amount_atomic_one_cent():
    s = Settings(x402_upto_max_amount="$0.01")
    assert s.x402_upto_max_amount_atomic == 10_000


def test_x402_upto_max_amount_atomic_no_dollar_sign():
    s = Settings(x402_upto_max_amount="0.25")
    assert s.x402_upto_max_amount_atomic == 250_000


def test_x402_upto_max_amount_atomic_unparseable_returns_zero():
    s = Settings(x402_upto_max_amount="invalid")
    assert s.x402_upto_max_amount_atomic == 0
