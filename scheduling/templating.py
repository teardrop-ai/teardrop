# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Safe prompt rendering for event-triggered runs.

Event triggers store a static prompt *template*; inbound payloads are
interpolated into ``{{placeholder}}`` slots. Substitution is intentionally
restricted to avoid prompt/format-string injection:

- Only ``{{key}}`` tokens matching a conservative character class are replaced.
- ``str.format`` is never used (it would expose attribute/index access such as
  ``{0.__class__}``); replacement is a plain regex substitution.
- Only scalar top-level values (str/int/float/bool) are inlined; nested
  structures resolve to empty unless requested explicitly via ``event_json``.
- The special key ``event_json`` yields a compact JSON encoding of the payload.
- Individual values and the final rendered prompt are length-capped.
"""

from __future__ import annotations

import json
import re

_PLACEHOLDER = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")
_VALUE_MAX_CHARS = 4_000


def _coerce_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value[:_VALUE_MAX_CHARS]
    return ""


def render_event_prompt(template: str, payload: object, *, max_chars: int) -> str:
    """Render *template* against *payload*, returning a bounded prompt string.

    Unknown or non-scalar placeholders resolve to an empty string. The reserved
    key ``event_json`` expands to a compact JSON dump of the full payload.
    """
    data: dict[str, object] = payload if isinstance(payload, dict) else {}

    try:
        event_json = json.dumps(payload, default=str, separators=(",", ":"))[:_VALUE_MAX_CHARS]
    except (TypeError, ValueError):
        event_json = ""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "event_json":
            return event_json
        return _coerce_scalar(data.get(key))

    rendered = _PLACEHOLDER.sub(_sub, template)
    return rendered[:max_chars]
