"""Generate an RSA-2048 keypair for JWT signing.

Usage:
    python scripts/generate_keys.py

Outputs:
    keys/private.pem
    keys/public.pem
"""

from __future__ import annotations

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_keypair(keys_dir: Path | None = None) -> None:
    """Generate an RSA-2048 keypair; no-op if keys already exist.

    Safe to call from application startup code – will not overwrite an
    existing key pair.
    """
    if keys_dir is None:
        keys_dir = Path(__file__).resolve().parent.parent / "keys"
    keys_dir.mkdir(exist_ok=True)

    private_path = keys_dir / "private.pem"
    public_path = keys_dir / "public.pem"

    if private_path.exists():
        return  # already generated – nothing to do

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


def main() -> None:
    keys_dir = Path(__file__).resolve().parent.parent / "keys"
    private_path = keys_dir / "private.pem"

    if private_path.exists():
        print(f"Key already exists at {private_path} — aborting.")
        print("Delete the keys/ directory first if you want to regenerate.")
        sys.exit(1)

    generate_keypair(keys_dir)


if __name__ == "__main__":
    main()
