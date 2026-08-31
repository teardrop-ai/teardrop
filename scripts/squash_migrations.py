# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Generate and install a deterministic SQL migration squash baseline."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_MIGRATION_NAME_RE = re.compile(r"(?P<prefix>[0-9]+)_[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


class SquashError(ValueError):
    """Raised when migration files cannot be safely squashed."""


def discover_migrations(migrations_dir: Path) -> list[Path]:
    """Return SQL migrations in the same order as the production runner."""
    return sorted(migrations_dir.glob("*.sql"), key=lambda path: path.name)


def _migration_prefix(path: Path) -> int:
    match = _MIGRATION_NAME_RE.fullmatch(path.stem)
    if match is None:
        raise SquashError(f"Invalid migration filename: {path.name}")
    return int(match.group("prefix"))


def select_migrations(migrations_dir: Path, cutoff: str) -> list[Path]:
    """Select every migration through the exact cutoff stem."""
    migrations = discover_migrations(migrations_dir)
    for index, path in enumerate(migrations):
        if path.stem == cutoff:
            return migrations[: index + 1]
    raise SquashError(f"Migration cutoff not found: {cutoff}")


def _manifest_entries(manifest: Path) -> list[str]:
    entries: list[str] = []
    for line_number, raw_line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue
        if _MIGRATION_NAME_RE.fullmatch(entry) is None:
            raise SquashError(f"Invalid migration stem in {manifest.name}:{line_number}")
        if entry in entries:
            raise SquashError(f"Duplicate migration {entry} in {manifest.name}")
        entries.append(entry)
    if not entries:
        raise SquashError(f"Squash manifest {manifest.name} is empty")
    return entries


def _history_for(stem: str, migrations_dir: Path, active: set[str], visiting: set[str]) -> list[str]:
    if stem in visiting:
        raise SquashError(f"Cyclic squash manifest involving {stem}")
    manifest = migrations_dir / f"{stem}.replaces"
    if not manifest.is_file():
        return [stem]

    visiting.add(stem)
    history: list[str] = []
    for entry in _manifest_entries(manifest):
        if entry in active:
            history.extend(_history_for(entry, migrations_dir, active, visiting))
        elif entry not in history:
            history.append(entry)
    visiting.remove(stem)
    if stem not in history:
        history.append(stem)
    return history


def _replacement_history(selected: list[Path], migrations_dir: Path) -> list[str]:
    active = {path.stem for path in discover_migrations(migrations_dir)}
    history: list[str] = []
    for path in selected:
        for stem in _history_for(path.stem, migrations_dir, active, set()):
            if stem not in history:
                history.append(stem)
    return history


def build_baseline(selected: list[Path], output_stem: str, replacements: list[str]) -> tuple[str, str]:
    """Build baseline SQL and its newline-delimited replacement manifest."""
    sections = [f"-- Squashed migration {output_stem}; source order is preserved."]
    for path in selected:
        source = path.read_text(encoding="utf-8").rstrip()
        sections.append(f"-- Source migration: {path.stem}\n{source}")
    sql = "\n\n".join(sections) + "\n"
    manifest = "\n".join(replacements) + "\n"
    return sql, manifest


def _next_output_stem(migrations: list[Path]) -> str:
    if not migrations:
        raise SquashError("No SQL migrations found")
    return f"{max(_migration_prefix(path) for path in migrations) + 1:03d}_squashed_baseline"


def install_squash(
    migrations_dir: Path,
    archive_dir: Path,
    selected: list[Path],
    output_stem: str,
    sql: str,
    manifest: str,
) -> None:
    output_sql = migrations_dir / f"{output_stem}.sql"
    output_manifest = migrations_dir / f"{output_stem}.replaces"
    if output_sql.exists() or output_manifest.exists():
        raise SquashError(f"Squash output already exists: {output_stem}")

    archive_targets = [archive_dir / path.name for path in selected]
    archive_targets.extend(
        archive_dir / f"{path.stem}.replaces" for path in selected if (migrations_dir / f"{path.stem}.replaces").is_file()
    )
    if any(target.exists() for target in archive_targets):
        raise SquashError("Archive already contains a selected migration")

    archive_dir.mkdir(parents=True, exist_ok=True)
    output_sql.write_text(sql, encoding="utf-8")
    output_manifest.write_text(manifest, encoding="utf-8")
    for path in selected:
        archive_path = archive_dir / path.name
        archive_path.write_bytes(path.read_bytes())
        if archive_path.read_bytes() != path.read_bytes():
            raise SquashError(f"Archive verification failed for {path.name}")
        sidecar = migrations_dir / f"{path.stem}.replaces"
        if sidecar.is_file():
            archive_sidecar = archive_dir / sidecar.name
            archive_sidecar.write_bytes(sidecar.read_bytes())
            if archive_sidecar.read_bytes() != sidecar.read_bytes():
                raise SquashError(f"Archive verification failed for {sidecar.name}")
    for path in selected:
        path.unlink()
        sidecar = migrations_dir / f"{path.stem}.replaces"
        if sidecar.is_file():
            sidecar.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a Teardrop migration squash baseline")
    parser.add_argument("--cutoff", required=True, help="Exact migration stem to include as the final source")
    parser.add_argument("--migrations-dir", type=Path, default=_DEFAULT_MIGRATIONS_DIR)
    parser.add_argument("--archive-dir", type=Path, default=None)
    parser.add_argument("--output-stem", default=None, help="Output stem; defaults to the next numeric prefix")
    parser.add_argument("--write", action="store_true", help="Install the baseline and archive its source files")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    migrations_dir = args.migrations_dir.resolve()
    archive_dir = (args.archive_dir or migrations_dir.parent / "archive").resolve()
    selected = select_migrations(migrations_dir, args.cutoff)
    output_stem = args.output_stem or _next_output_stem(discover_migrations(migrations_dir))
    if _MIGRATION_NAME_RE.fullmatch(output_stem) is None:
        raise SystemExit(f"Invalid output stem: {output_stem}")
    replacements = _replacement_history(selected, migrations_dir)
    sql, manifest = build_baseline(selected, output_stem, replacements)

    print(f"Baseline: {output_stem}.sql")
    print(f"Sources: {len(selected)} migration(s)")
    print(f"Replacements: {len(replacements)} migration stem(s)")
    if args.write:
        install_squash(migrations_dir, archive_dir, selected, output_stem, sql, manifest)
        print(f"Archived sources in {archive_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
