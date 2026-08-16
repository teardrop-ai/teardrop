# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Generic registry-backed validation for machine-readable agent outputs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from shared.json_output import extract_first_json_object

ETH_PRIMITIVE_FEES_TASK = "eth_primitive_fees"
_CONTRACT_PATTERN = re.compile(r"\b(?:OUTPUT_CONTRACT|TASK_CLASS)\s*:\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", re.IGNORECASE)
_CONTRACT_DIR = Path(__file__).with_name("contracts")


@dataclass(frozen=True)
class OutputContract:
    task_class: str
    schema_version: int
    schema: dict[str, Any]
    validator: Draft202012Validator

    @property
    def repair_prompt(self) -> str:
        schema_text = json.dumps(self.schema, separators=(",", ":"), ensure_ascii=True)
        return (
            f"Return ONLY one valid JSON object for output contract {self.task_class} "
            f"schema_version {self.schema_version}. Do not emit analysis, markdown fences, "
            "or prose before the object. Use only observed tool data; preserve nulls and "
            f"never invent values. Validate against this JSON Schema: {schema_text}"
        )

    def validate(self, payload: dict[str, Any]) -> bool:
        try:
            self.validator.validate(payload)
        except ValidationError:
            return False
        return True


def _load_contracts() -> dict[str, OutputContract]:
    contracts: dict[str, OutputContract] = {}
    for path in sorted(_CONTRACT_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        task_class = schema["properties"]["task_class"]["const"]
        schema_version = schema["properties"]["schema_version"]["const"]
        if not isinstance(task_class, str) or not isinstance(schema_version, int):
            raise ValueError(f"Invalid output contract metadata in {path.name}")
        if task_class in contracts:
            raise ValueError(f"Duplicate output contract: {task_class}")
        contracts[task_class] = OutputContract(
            task_class=task_class,
            schema_version=schema_version,
            schema=schema,
            validator=Draft202012Validator(schema),
        )
    return contracts


_CONTRACTS = _load_contracts()


def get_output_contract(task_class: str) -> OutputContract | None:
    return _CONTRACTS.get(task_class)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
            for block in content
            if (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) == "text"
        )
    return str(content or "")


def detect_output_contract(messages: list[Any]) -> OutputContract | None:
    """Resolve a trusted output contract from the current human turn."""
    for message in reversed(messages):
        if getattr(message, "type", "") not in {"human", "user"}:
            continue
        matches = list(_CONTRACT_PATTERN.finditer(_content_to_text(getattr(message, "content", ""))))
        for match in reversed(matches):
            contract = get_output_contract(match.group(1))
            if contract is not None:
                return contract
        return None
    return None


def normalize_output(contract: OutputContract, text: str) -> str | None:
    """Validate and canonicalize a contract object while preserving its report suffix."""
    extracted = extract_first_json_object(text)
    if extracted is None:
        return None
    payload, _, end = extracted
    if not contract.validate(payload):
        return None

    canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    suffix = text[end:].strip()
    if not suffix:
        return canonical
    if suffix.startswith("---"):
        return f"{canonical}\n{suffix}"
    return f"{canonical}\n---\n{suffix}"


def build_contract_failure(contract: OutputContract) -> str:
    """Return parseable failure data without manufacturing a domain label."""
    payload = {
        "task_class": contract.task_class,
        "schema_version": contract.schema_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract_status": "validation_failed",
        "data_gaps": ["OUTPUT_CONTRACT_VALIDATION_FAILED"],
    }
    return (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        + "\n---\nNo labels emitted because the output contract could not be validated."
    )


def normalize_eth_primitive_output(text: str) -> str | None:
    """Backward-compatible wrapper for callers migrating to ``normalize_output``."""
    contract = get_output_contract(ETH_PRIMITIVE_FEES_TASK)
    return normalize_output(contract, text) if contract is not None else None


def build_eth_primitive_fallback() -> str:
    """Backward-compatible failure wrapper; it intentionally emits no labels."""
    contract = get_output_contract(ETH_PRIMITIVE_FEES_TASK)
    if contract is None:
        return json.dumps({"contract_status": "validation_failed"})
    return build_contract_failure(contract)
