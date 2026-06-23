from __future__ import annotations

from tools.shared import normalize_to_safe_schema_subset


def test_normalize_strips_unsupported_keywords_and_nullable_types() -> None:
    normalized, dropped = normalize_to_safe_schema_subset(
        {
            "type": "object",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "properties": {
                "query": {
                    "type": ["string", "null"],
                    "format": "uri",
                    "minLength": 3,
                },
                "mode": {
                    "anyOf": [{"type": "string"}, {"type": "integer"}],
                },
            },
            "required": ["query", "missing"],
            "additionalProperties": False,
        }
    )

    assert normalized == {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 3},
            "mode": {"type": "string"},
        },
        "required": ["query"],
    }
    assert any("$schema" in item for item in dropped)
    assert any("null" in item for item in dropped)
    assert any("anyOf" in item for item in dropped)
    assert any("additionalProperties" in item for item in dropped)


def test_normalize_defaults_empty_root_to_safe_object() -> None:
    normalized, dropped = normalize_to_safe_schema_subset({})

    assert normalized == {"type": "object", "properties": {}}
    assert dropped == []


def test_normalize_preserves_supported_nested_shapes() -> None:
    normalized, dropped = normalize_to_safe_schema_subset(
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "count": {"type": "integer", "minimum": 1, "maximum": 9},
                        },
                        "required": ["count"],
                    },
                }
            },
            "required": ["items"],
        }
    )

    assert normalized["properties"]["items"]["items"] == {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 9},
        },
        "required": ["count"],
    }
    assert dropped == []
