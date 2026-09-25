# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Fernet encryption for webhook and MCP auth header values."""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from teardrop.config import get_settings


@lru_cache(maxsize=8)
def _fernet_for_key(key: str) -> Fernet:
    return Fernet(key.encode())


def _get_org_tool_fernet() -> Fernet:
    settings = get_settings()
    key = settings.org_tool_encryption_key
    if not key:
        raise RuntimeError(
            "ORG_TOOL_ENCRYPTION_KEY is not set — generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return _fernet_for_key(key)


def encrypt_header_value(value: str) -> str:
    """Encrypt a webhook or MCP auth header/token value."""
    return _get_org_tool_fernet().encrypt(value.encode()).decode()


def decrypt_header_value(encrypted: str) -> str:
    """Decrypt an encrypted webhook or MCP auth header/token value."""
    return _get_org_tool_fernet().decrypt(encrypted.encode()).decode()
