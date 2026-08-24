# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for JWT signing key generation."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization

from teardrop.keys import generate_keypair


def test_generate_keypair_writes_matching_rsa_pems(tmp_path):
    generate_keypair(tmp_path)

    private_key = serialization.load_pem_private_key((tmp_path / "private.pem").read_bytes(), password=None)
    public_key = serialization.load_pem_public_key((tmp_path / "public.pem").read_bytes())

    assert private_key.key_size == 2048
    assert private_key.public_key().public_numbers() == public_key.public_numbers()


def test_generate_keypair_does_not_overwrite_existing_keys(tmp_path):
    generate_keypair(tmp_path)
    private_before = (tmp_path / "private.pem").read_bytes()
    public_before = (tmp_path / "public.pem").read_bytes()

    generate_keypair(tmp_path)

    assert (tmp_path / "private.pem").read_bytes() == private_before
    assert (tmp_path / "public.pem").read_bytes() == public_before
