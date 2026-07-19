# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Bump the backend ``APP_VERSION`` and regenerate spec artifacts.

``teardrop/_meta.py`` is the single source of truth for the backend version.
This script updates it, then re-runs ``scripts.export_api_spec`` so that
``spec/openapi.json`` and ``spec/events.schema.json`` stay in sync.

Usage:
    python scripts/bump_version.py patch
    python scripts/bump_version.py minor --dry-run
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

META_FILE = Path(__file__).resolve().parent.parent / "teardrop" / "_meta.py"
VERSION_RE = re.compile(r'^(APP_VERSION\s*=\s*")(?P<version>\d+\.\d+\.\d+)(")', re.MULTILINE)


def parse_version(version: str) -> tuple[int, int, int]:
    """Parse a SemVer string into a tuple of integers."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Expected SemVer (MAJOR.MINOR.PATCH), got: {version!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def bump_version(current: str, kind: str) -> str:
    """Return the next version string for the given bump kind."""
    major, minor, patch = parse_version(current)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unknown bump kind: {kind!r}")


def read_current_version() -> str:
    """Read ``APP_VERSION`` from ``teardrop/_meta.py``."""
    text = META_FILE.read_text()
    match = VERSION_RE.search(text)
    if not match:
        raise RuntimeError(f"Could not find APP_VERSION in {META_FILE}")
    return match.group("version")


def write_version(new_version: str) -> None:
    """Update ``APP_VERSION`` in ``teardrop/_meta.py``."""
    text = META_FILE.read_text()
    new_text, count = VERSION_RE.subn(lambda m: f"{m.group(1)}{new_version}{m.group(3)}", text)
    if count != 1:
        raise RuntimeError(f"Expected one APP_VERSION assignment, replaced {count}")
    META_FILE.write_text(new_text)


def export_spec() -> None:
    """Regenerate OpenAPI and event schema artifacts."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.export_api_spec"],
        cwd=META_FILE.parent.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"export_api_spec failed:\n{result.stderr}")
    print(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump the backend APP_VERSION.")
    parser.add_argument("kind", choices=["major", "minor", "patch"], help="SemVer component to bump")
    parser.add_argument("--dry-run", action="store_true", help="Print the new version without changing files")
    args = parser.parse_args()

    current = read_current_version()
    new = bump_version(current, args.kind)

    print(f"Current version: {current}")
    print(f"New version:     {new}")

    if args.dry_run:
        print("Dry run: no files modified.")
        return 0

    write_version(new)
    print(f"Updated {META_FILE}")

    export_spec()

    print("\nNext steps:")
    print("  git add teardrop/_meta.py spec/")
    print(f"  git commit -m 'chore(release): bump version to {new}'")
    print(f"  git tag v{new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
