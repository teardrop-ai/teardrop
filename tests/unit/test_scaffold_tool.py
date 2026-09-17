# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for scripts/scaffold_tool.py — agent-consumer tool scaffolding."""

from __future__ import annotations

import ast

import pytest

from scripts.scaffold_tool import build_tool_module


def _build(**overrides):
    defaults = {
        "name": "get_example",
        "description": "Return the example metric for a wallet.",
        "use_when": "Use when a wallet's example metric is needed before committing funds.",
        "limitations": "Covers Ethereum and Base only; data may lag the chain tip.",
        "alternatives": ["get_wallet_portfolio"],
        "tags": ["web3"],
    }
    defaults.update(overrides)
    return build_tool_module(**defaults)


def test_generated_module_is_valid_python_with_required_fields():
    source = _build()
    ast.parse(source)  # raises SyntaxError if the template renders invalid Python
    for field in ("use_when", "limitations", "alternatives", "description"):
        assert field in source
    assert "get_example" in source
    assert "GetExampleInput" in source
    assert "GetExampleOutput" in source


def test_rejects_non_snake_case_name():
    with pytest.raises(ValueError, match="snake_case"):
        _build(name="GetExample")


def test_alternatives_and_tags_rendered():
    source = _build(alternatives=["get_wallet_portfolio", "get_erc20_balance"], tags=["web3", "balance"])
    assert "get_wallet_portfolio" in source
    assert "get_erc20_balance" in source
    assert "'balance'" in source or '"balance"' in source
