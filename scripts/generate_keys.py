# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Compatibility CLI for generating the JWT signing keypair."""

from __future__ import annotations

import sys
from pathlib import Path

from teardrop.keys import generate_keypair


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
