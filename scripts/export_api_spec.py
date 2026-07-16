# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Export machine-readable API spec artifacts for downstream SDK codegen.

The FastAPI app (``teardrop.app:app``) already generates ``openapi.json`` for
REST routes. This script snapshots that OpenAPI document alongside a
JSON-Schema description of the ``/agent/run`` SSE event contract (which
OpenAPI cannot express), so both can be committed/diffed in version control
and consumed by downstream SDK codegen pipelines.

Usage:
    .venv\\Scripts\\python -m scripts.export_api_spec

Outputs:
    spec/openapi.json        — REST endpoint contract (FastAPI-generated)
    spec/events.schema.json  — SSE event payload contract (hand-maintained
                                models in teardrop/agent_stream.py)
"""

from __future__ import annotations

import json
from pathlib import Path


def export_spec(out_dir: Path | None = None) -> tuple[Path, Path]:
    """Write ``openapi.json`` and ``events.schema.json`` into ``out_dir``.

    Returns the paths written. Safe to call repeatedly — overwrites in place.
    """
    if out_dir is None:
        out_dir = Path(__file__).resolve().parent.parent / "spec"
    out_dir.mkdir(exist_ok=True)

    from teardrop._meta import APP_VERSION
    from teardrop.agent_stream import get_event_json_schemas
    from teardrop.app import app

    openapi_path = out_dir / "openapi.json"
    openapi_path.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")

    events_path = out_dir / "events.schema.json"
    events_doc = {"version": APP_VERSION, "events": get_event_json_schemas()}
    events_path.write_text(json.dumps(events_doc, indent=2, sort_keys=True) + "\n")

    return openapi_path, events_path


if __name__ == "__main__":
    written = export_spec()
    for path in written:
        print(f"wrote {path}")
