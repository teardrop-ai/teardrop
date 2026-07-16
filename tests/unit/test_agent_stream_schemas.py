# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for SSE event JSON-schema export in ``teardrop.agent_stream``."""

from __future__ import annotations

import json

from teardrop import agent_stream
from teardrop.agent_stream import EVENT_SCHEMAS, get_event_json_schemas

# Every _EV_* constant module-level name, resolved to its string value.
_ALL_EVENT_TYPES = {
    name: value for name, value in vars(agent_stream).items() if name.startswith("_EV_") and isinstance(value, str)
}


def test_every_event_constant_has_a_registry_entry():
    for name, value in _ALL_EVENT_TYPES.items():
        assert value in EVENT_SCHEMAS, f"{name} ({value!r}) missing from EVENT_SCHEMAS"


def test_schemas_are_json_serializable():
    schemas = get_event_json_schemas()
    # Must not raise, and must round-trip through json.
    dumped = json.dumps(schemas)
    reloaded = json.loads(dumped)
    assert reloaded.keys() == schemas.keys()


def test_custom_event_has_both_known_variants():
    schemas = get_event_json_schemas()
    custom_schemas = schemas[agent_stream._EV_CUSTOM]
    assert isinstance(custom_schemas, list)
    assert len(custom_schemas) == 2
    titles = {s.get("title") for s in custom_schemas}
    assert titles == {"ToolOutputCustomEvent", "AgentWarningCustomEvent"}


def test_usage_summary_schema_has_expected_fields():
    schemas = get_event_json_schemas()
    usage_schema = schemas[agent_stream._EV_USAGE_SUMMARY]
    assert set(usage_schema["properties"]) == {
        "run_id",
        "tokens_in",
        "tokens_out",
        "cache_read_tokens",
        "cache_creation_tokens",
        "tool_calls",
        "duration_ms",
        "cost_usdc",
        "platform_fee_usdc",
        "delegation_cost_usdc",
    }
