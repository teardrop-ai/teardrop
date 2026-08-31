# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Validate that active SQL migrations use unique numeric prefixes."""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

_PREFIX_RE = re.compile(r"(?P<prefix>[0-9]+)_")
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def duplicate_prefixes(migrations_dir: Path) -> dict[str, list[str]]:
    prefixes: defaultdict[str, list[str]] = defaultdict(list)
    for path in sorted(migrations_dir.glob("*.sql"), key=lambda item: item.name):
        match = _PREFIX_RE.match(path.name)
        if match is None:
            raise ValueError(f"Migration filename has no numeric prefix: {path.name}")
        prefixes[match.group("prefix")].append(path.name)
    return {prefix: names for prefix, names in prefixes.items() if len(names) > 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check active migration numeric prefixes")
    parser.add_argument("--migrations-dir", type=Path, default=_DEFAULT_MIGRATIONS_DIR)
    args = parser.parse_args(argv)

    duplicates = duplicate_prefixes(args.migrations_dir)
    if duplicates:
        for prefix, names in sorted(duplicates.items()):
            print(f"Duplicate migration prefix {prefix}: {', '.join(names)}")
        return 1
    print(f"Migration prefixes are unique in {args.migrations_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
