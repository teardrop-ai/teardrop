# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scaffold a new tool definition with the agent-consumer contract required.

Agent-commerce fields (use_when, limitations, alternatives) are mandatory CLI
arguments so new tools cannot be created without them. Dry-run by default;
pass --write to create tools/definitions/<name>.py.

Usage:
    python scripts/scaffold_tool.py \
        --name get_example \
        --description "Return the example metric for a wallet." \
        --use-when "Use when a wallet's example metric is needed before committing funds." \
        --limitations "Covers Ethereum and Base only; data may lag the chain tip." \
        --alternative get_wallet_portfolio \
        --tags web3 example \
        --write
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_TEMPLATE = '''# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""{name} – {one_line}."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tools.registry import ToolDefinition

# ─── Schemas ──────────────────────────────────────────────────────────────────


class {class_name}Input(BaseModel):
    pass


class {class_name}Output(BaseModel):
    pass


# ─── Implementation ──────────────────────────────────────────────────────────


async def {name}() -> dict[str, Any]:
    """Implement the tool; return a dict matching {class_name}Output."""
    raise NotImplementedError


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="{name}",
    version="1.0.0",
    description=(
        {description!r}
    ),
    tags={tags!r},
    use_when=(
        {use_when!r}
    ),
    limitations=(
        {limitations!r}
    ),
    alternatives={alternatives!r},
    input_schema={class_name}Input,
    output_schema={class_name}Output,
    implementation={name},
)
'''


def build_tool_module(
    *,
    name: str,
    description: str,
    use_when: str,
    limitations: str,
    alternatives: list[str],
    tags: list[str],
) -> str:
    """Render the tool-definition module source for the given metadata."""
    if not _SNAKE_RE.fullmatch(name):
        raise ValueError(f"Tool name must be snake_case: {name!r}")
    class_name = "".join(part.capitalize() for part in name.split("_"))
    one_line = description.splitlines()[0].rstrip(".")
    return _TEMPLATE.format(
        name=name,
        class_name=class_name,
        one_line=one_line,
        description=description,
        use_when=use_when,
        limitations=limitations,
        alternatives=alternatives,
        tags=tags,
    )


def main() -> None:
    # The template contains box-drawing characters that break a redirected cp1252 stdout.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="snake_case tool name")
    parser.add_argument("--description", required=True, help="Decision-oriented description (agent consumer)")
    parser.add_argument("--use-when", required=True, help="When an agent should select this tool")
    parser.add_argument("--limitations", required=True, help="Staleness, caps, and what the output does not prove")
    parser.add_argument(
        "--alternative",
        action="append",
        default=[],
        dest="alternatives",
        help="Registered alternative tool name (repeatable)",
    )
    parser.add_argument("--tag", action="append", default=[], dest="tags", help="Categorisation tag (repeatable)")
    parser.add_argument("--write", action="store_true", help="Write tools/definitions/<name>.py (default: dry-run)")
    args = parser.parse_args()

    if not args.alternatives:
        parser.error("at least one --alternative is required")
    if not args.tags:
        parser.error("at least one --tag is required")

    content = build_tool_module(
        name=args.name,
        description=args.description,
        use_when=args.use_when,
        limitations=args.limitations,
        alternatives=args.alternatives,
        tags=args.tags,
    )

    if args.write:
        path = Path(__file__).resolve().parent.parent / "tools" / "definitions" / f"{args.name}.py"
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path}")
        print(
            "Next: import and register TOOL in tools/definitions/__init__.py, "
            "then run tests/unit/test_tool_definition_standard.py"
        )
    else:
        print(content)
        print("# Dry run — pass --write to create the file.")


if __name__ == "__main__":
    main()
