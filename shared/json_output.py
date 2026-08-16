# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared parsing helpers for JSON-first agent responses."""

from __future__ import annotations

import re
from json import JSONDecodeError, JSONDecoder
from typing import Any


def extract_first_json_object(text: str) -> tuple[dict[str, Any], int, int] | None:
    """Extract the first valid JSON object and its character bounds."""
    decoder = JSONDecoder()
    for match in re.finditer(r"\{", text):
        start = match.start()
        try:
            payload, length = decoder.raw_decode(text[start:])
        except JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, start, start + length
    return None
