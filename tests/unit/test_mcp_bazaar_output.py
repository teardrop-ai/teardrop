# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-tool Bazaar output declarations on MCP 402 challenges."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from x402.extensions.bazaar.facilitator import validate_discovery_extension_spec
from x402.schemas.payments import PaymentRequirements

import teardrop.mcp_gateway as gateway
from tools import registry

_HEADER_BUDGET_BYTES = 8_192
_TOOL_NAMES = sorted(tool.name for tool in registry.list_latest())


def _requirement() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        amount="5000",
        pay_to="0x" + "1" * 40,
        max_timeout_seconds=300,
        extra={"name": "USD Coin", "version": "2"},
    )


def _output_schema(tool_name: str) -> dict:
    model = registry.get(tool_name).output_schema
    return model if isinstance(model, dict) else model.model_json_schema()


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_tool_declaration_is_spec_valid_and_fits_header_budget(tool_name, monkeypatch):
    from billing.x402 import build_402_headers

    monkeypatch.setattr(gateway, "public_base_url", lambda request, settings: "https://api.teardrop.dev")
    extension = gateway._mcp_402_extensions(tool_name)

    # The spec check also validates the generated example against the declared output schema.
    assert validate_discovery_extension_spec(extension["bazaar"]).valid
    headers = build_402_headers(
        resource=gateway._mcp_402_resource(SimpleNamespace()), extensions=extension, requirements=[_requirement()]
    )
    assert sum(len(value) for value in headers.values()) < _HEADER_BUDGET_BYTES


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_object_root_output_schemas_are_declared(tool_name):
    output = gateway._mcp_402_extensions(tool_name)["bazaar"]["info"].get("output")
    declared = _output_schema(tool_name).get("type") == "object"

    assert (output is not None) is declared
    if declared:
        assert output["type"] == "json"
        assert isinstance(output["example"], dict)


def test_declared_schema_drops_pydantic_titles():
    schema = gateway._mcp_402_extensions("get_token_price")["bazaar"]["schema"]["properties"]["output"]["properties"]

    assert '"title"' not in str(schema).replace("'", '"')
    assert schema["example"]["type"] == "object"


def test_oversized_output_schema_falls_back_to_top_level_fields(monkeypatch):
    import json

    from tools.schema import flatten_embedded_json_schema

    full = gateway._without_titles(flatten_embedded_json_schema(_output_schema("get_wallet_positions")))
    shallow_chars = len(json.dumps(gateway._top_level_schema(full), separators=(",", ":")))
    assert shallow_chars < len(json.dumps(full, separators=(",", ":")))
    monkeypatch.setattr(gateway, "_BAZAAR_OUTPUT_SCHEMA_MAX_CHARS", shallow_chars)
    gateway._bazaar_output.cache_clear()
    try:
        output = gateway._bazaar_output("get_wallet_positions", registry.get("get_wallet_positions").version)
    finally:
        gateway._bazaar_output.cache_clear()

    assert output is not None
    assert all(set(field) <= {"type", "description"} for field in output.schema["properties"].values())
    assert set(output.example) == set(output.schema.get("required", []))


def test_output_schema_omitted_when_even_top_level_fields_are_too_large(monkeypatch):
    monkeypatch.setattr(gateway, "_BAZAAR_OUTPUT_SCHEMA_MAX_CHARS", 10)
    gateway._bazaar_output.cache_clear()
    try:
        extension = gateway._mcp_402_extensions("get_token_price")
    finally:
        gateway._bazaar_output.cache_clear()

    assert "output" not in extension["bazaar"]["info"]
    assert validate_discovery_extension_spec(extension["bazaar"]).valid


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}, "required": ["a"]}, {"a": ""}),
        ({"type": "array", "items": {"type": "number"}, "minItems": 2}, [0, 0]),
        ({"anyOf": [{"type": "null"}, {"type": "boolean"}]}, False),
        ({"type": ["null", "integer"]}, 0),
        ({"enum": ["low", "high"]}, "low"),
        ({"type": "string", "default": "usd"}, "usd"),
    ],
)
def test_schema_example(schema, expected):
    assert gateway._schema_example(schema) == expected
