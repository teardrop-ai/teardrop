# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared helpers for webhook and MCP tool integrations.

This module centralizes common logic used by org_tools, marketplace,
and mcp_client so those modules can avoid cross-importing private helpers.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from cryptography.fernet import Fernet
from jsonschema import Draft7Validator
from pydantic import BaseModel, Field, create_model

from teardrop.config import get_settings

_JSON_SCHEMA_TYPE_MAP: dict[str, type] = {
    "object": dict,
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
}

_SAFE_SCHEMA_KEYS: set[str] = {
    "type",
    "properties",
    "required",
    "description",
    "title",
    "default",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "items",
}

_SAFE_SCHEMA_TYPES: set[str] = {"object", "string", "integer", "number", "boolean", "array"}


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


def build_pydantic_model(
    name: str,
    schema: dict[str, Any],
    model_name: str | None = None,
) -> type[BaseModel]:
    """Create a Pydantic model from a JSON Schema 'properties' dict.

    JSON Schema validation keywords (``enum``, ``minimum``/``maximum``,
    ``minLength``/``maxLength``, ``pattern``) are translated into the
    equivalent Pydantic ``Field`` constraints so they are enforced at tool
    invocation time. Schemas that omit these keywords are unaffected, keeping
    the builder backward-compatible for existing tools.
    """
    properties = schema.get("properties", {})
    required_set = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    for field_name, field_def in properties.items():
        json_type = field_def.get("type", "string")
        py_type = _JSON_SCHEMA_TYPE_MAP.get(json_type, str)
        description = field_def.get("description", "")

        # Array handling ensures generated schema has non-empty `items`.
        if json_type == "array":
            items_def = field_def.get("items")
            if items_def and isinstance(items_def, dict):
                item_json_type = items_def.get("type", "string")
                item_py_type = _JSON_SCHEMA_TYPE_MAP.get(item_json_type, str)
                py_type = list[item_py_type]  # type: ignore[valid-type]
            else:
                py_type = list[str]

        # enum constrains the value to a fixed set; model it as a Literal so the
        # constraint is enforced regardless of the declared base type.
        enum_values = field_def.get("enum")
        if isinstance(enum_values, list) and enum_values:
            py_type = Literal[tuple(enum_values)]  # type: ignore[valid-type]

        field_kwargs = _build_field_constraints(field_def, json_type)

        if field_name in required_set:
            fields[field_name] = (py_type, Field(..., description=description, **field_kwargs))
        else:
            fields[field_name] = (py_type | None, Field(default=None, description=description, **field_kwargs))

    cls_name = model_name if model_name is not None else f"OrgTool_{name}_Input"
    return create_model(cls_name, **fields)


def _build_field_constraints(field_def: dict[str, Any], json_type: str) -> dict[str, Any]:
    """Translate JSON Schema validation keywords into Pydantic Field kwargs.

    Only keywords present in ``field_def`` are emitted, so fields without
    constraints get an empty kwargs dict and behave exactly as before.
    Numeric bounds apply to integer/number types; length/pattern apply to
    strings.
    """
    kwargs: dict[str, Any] = {}

    if json_type in ("integer", "number"):
        if "minimum" in field_def:
            kwargs["ge"] = field_def["minimum"]
        if "maximum" in field_def:
            kwargs["le"] = field_def["maximum"]
    elif json_type == "string":
        if "minLength" in field_def:
            kwargs["min_length"] = field_def["minLength"]
        if "maxLength" in field_def:
            kwargs["max_length"] = field_def["maxLength"]
        if field_def.get("pattern"):
            kwargs["pattern"] = field_def["pattern"]

    return kwargs


def validate_safe_schema_subset(schema: dict[str, Any]) -> list[str]:
    """Return unsupported schema keywords/types that runtime tooling cannot enforce."""
    errors: list[str] = []

    def _walk(node: Any, path: str, *, in_properties_map: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if in_properties_map:
                    _walk(value, f"{path}.{key}")
                    continue
                if key not in _SAFE_SCHEMA_KEYS:
                    errors.append(f"{path}.{key}: keyword not supported")
                if key == "type":
                    if isinstance(value, str):
                        if value not in _SAFE_SCHEMA_TYPES:
                            errors.append(f"{path}.type={value}: type not supported")
                    elif isinstance(value, list):
                        bad = [t for t in value if t not in _SAFE_SCHEMA_TYPES]
                        if bad:
                            errors.append(f"{path}.type={bad}: type not supported")
                    else:
                        errors.append(f"{path}.type: invalid type declaration")
                _walk(value, f"{path}.{key}", in_properties_map=(key == "properties"))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]", in_properties_map=False)

    try:
        Draft7Validator.check_schema(schema)
    except Exception:
        return ["$.schema: invalid JSON Schema"]

    _walk(schema, "$")
    return errors
