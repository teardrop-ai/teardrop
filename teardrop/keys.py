# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Generate and persist the RSA keypair used for JWT signing."""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_keypair(keys_dir: Path | None = None) -> None:
    """Generate an RSA-2048 keypair; no-op if keys already exist.

    Safe to call from application startup code - will not overwrite an
    existing key pair.
    """
    if keys_dir is None:
        keys_dir = Path(__file__).resolve().parent.parent / "keys"
    keys_dir.mkdir(exist_ok=True)

    private_path = keys_dir / "private.pem"
    public_path = keys_dir / "public.pem"

    if private_path.exists():
        return

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    print(f"Generated RSA-2048 keypair:\n  {private_path}\n  {public_path}")
