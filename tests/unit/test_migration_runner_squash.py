# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Tests for manifest-driven migration squashes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import migrations.runner as migration_runner
import scripts.check_migration_prefixes as prefix_checker
import scripts.squash_migrations as squash_migrations


def _pool_for(connection: MagicMock) -> MagicMock:
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield connection

    pool.acquire = acquire
    return pool


def _connection_with_applied(versions: list[str]) -> MagicMock:
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetch = AsyncMock(return_value=[{"version": version} for version in versions])
    connection.executemany = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield

    connection.transaction = transaction
    return connection


def _write_squash(versions_dir, *, baseline_sql: str = "SELECT 1;\n") -> tuple[str, ...]:
    versions_dir.mkdir()
    replaced = ("001_first", "001_second", "009_tool_pricing_overrides")
    (versions_dir / "102_squashed_baseline.sql").write_text(baseline_sql, encoding="utf-8")
    (versions_dir / "102_squashed_baseline.replaces").write_text("\n".join(replaced) + "\n", encoding="utf-8")
    return replaced


def test_squash_generator_preserves_source_order_and_manifest(tmp_path) -> None:
    migrations_dir = tmp_path / "versions"
    migrations_dir.mkdir()
    first = migrations_dir / "009_a2a_delegation.sql"
    second = migrations_dir / "009_tool_pricing_overrides.sql"
    first.write_text("CREATE TABLE first (id integer);\n", encoding="utf-8")
    second.write_text("CREATE TABLE second (id integer);\n", encoding="utf-8")

    selected = squash_migrations.select_migrations(migrations_dir, first.stem)
    sql, manifest = squash_migrations.build_baseline(selected, "010_squashed_baseline", [path.stem for path in selected])

    assert [path.name for path in selected] == [first.name]
    assert sql.index("Source migration: 009_a2a_delegation") < sql.index("CREATE TABLE first")
    assert manifest == "009_a2a_delegation\n"


def test_squash_generator_rejects_missing_cutoff(tmp_path) -> None:
    migrations_dir = tmp_path / "versions"
    migrations_dir.mkdir()
    (migrations_dir / "001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")

    with pytest.raises(squash_migrations.SquashError, match="Migration cutoff not found"):
        squash_migrations.select_migrations(migrations_dir, "002_missing")


def test_squash_generator_includes_prior_baseline_history(tmp_path) -> None:
    migrations_dir = tmp_path / "versions"
    migrations_dir.mkdir()
    (migrations_dir / "102_squashed_baseline.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (migrations_dir / "102_squashed_baseline.replaces").write_text("001_first\n002_second\n", encoding="utf-8")
    (migrations_dir / "103_new_feature.sql").write_text("SELECT 2;\n", encoding="utf-8")

    selected = squash_migrations.select_migrations(migrations_dir, "103_new_feature")
    replacements = squash_migrations._replacement_history(selected, migrations_dir)

    assert replacements == ["001_first", "002_second", "102_squashed_baseline", "103_new_feature"]


def test_install_squash_verifies_archive_before_removing_sources(tmp_path) -> None:
    migrations_dir = tmp_path / "versions"
    archive_dir = tmp_path / "archive"
    migrations_dir.mkdir()
    source = migrations_dir / "001_first.sql"
    source.write_text("CREATE TABLE first (id integer);\n", encoding="utf-8")

    sql, manifest = squash_migrations.build_baseline([source], "002_squashed_baseline", [source.stem])
    squash_migrations.install_squash(migrations_dir, archive_dir, [source], "002_squashed_baseline", sql, manifest)

    assert not source.exists()
    assert (archive_dir / source.name).read_text(encoding="utf-8") == "CREATE TABLE first (id integer);\n"
    assert (migrations_dir / "002_squashed_baseline.sql").read_text(encoding="utf-8") == sql
    assert (migrations_dir / "002_squashed_baseline.replaces").read_text(encoding="utf-8") == manifest


def test_prefix_checker_ignores_archived_prefix_collisions(tmp_path) -> None:
    versions_dir = tmp_path / "versions"
    versions_dir.mkdir()
    (versions_dir / "102_squashed_baseline.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (versions_dir / "102_squashed_baseline.replaces").write_text("001_first\n", encoding="utf-8")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "009_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "archive" / "009_second.sql").write_text("SELECT 1;\n", encoding="utf-8")

    assert prefix_checker.duplicate_prefixes(versions_dir) == {}


@pytest.mark.asyncio
async def test_runner_releases_advisory_lock_when_application_fails(tmp_path, monkeypatch) -> None:
    versions_dir = tmp_path / "versions"
    versions_dir.mkdir()
    (versions_dir / "102_squashed_baseline.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (versions_dir / "102_squashed_baseline.replaces").write_text("001_first\n", encoding="utf-8")
    monkeypatch.setattr(migration_runner, "_VERSIONS_DIR", versions_dir)
    connection = _connection_with_applied([])
    connection.execute.side_effect = [None, RuntimeError("database unavailable"), None]

    with pytest.raises(RuntimeError, match="database unavailable"):
        await migration_runner.apply_pending(_pool_for(connection))

    assert connection.execute.await_args_list[-1].args == ("SELECT pg_advisory_unlock($1)", migration_runner._ADVISORY_LOCK_KEY)


@pytest.mark.asyncio
async def test_fresh_database_applies_baseline_and_records_replaced_stems(tmp_path, monkeypatch) -> None:
    versions_dir = tmp_path / "versions"
    replaced = _write_squash(versions_dir, baseline_sql="CREATE TABLE example (id integer);\n")
    monkeypatch.setattr(migration_runner, "_VERSIONS_DIR", versions_dir)
    connection = _connection_with_applied([])

    applied = await migration_runner.apply_pending(_pool_for(connection))

    assert applied == ["102_squashed_baseline"]
    assert connection.executemany.await_args.args[1] == [
        ("102_squashed_baseline",),
        *((version,) for version in replaced),
    ]


@pytest.mark.asyncio
async def test_existing_database_records_baseline_without_replaying_sql(tmp_path, monkeypatch) -> None:
    versions_dir = tmp_path / "versions"
    replaced = _write_squash(versions_dir, baseline_sql="CREATE TABLE must_not_run (id integer);\n")
    monkeypatch.setattr(migration_runner, "_VERSIONS_DIR", versions_dir)
    connection = _connection_with_applied(list(replaced))

    applied = await migration_runner.apply_pending(_pool_for(connection))

    assert applied == ["102_squashed_baseline"]
    assert connection.executemany.await_args.args[1] == [("102_squashed_baseline",)]
    assert all(call.args[0] != "CREATE TABLE must_not_run (id integer);\n" for call in connection.execute.await_args_list)


@pytest.mark.asyncio
async def test_partial_database_history_fails_before_running_baseline(tmp_path, monkeypatch) -> None:
    versions_dir = tmp_path / "versions"
    replaced = _write_squash(versions_dir, baseline_sql="CREATE TABLE must_not_run (id integer);\n")
    monkeypatch.setattr(migration_runner, "_VERSIONS_DIR", versions_dir)
    connection = _connection_with_applied([replaced[0]])

    with pytest.raises(RuntimeError, match="001_second"):
        await migration_runner.apply_pending(_pool_for(connection))

    connection.executemany.assert_not_awaited()
    assert all(call.args[0] != "CREATE TABLE must_not_run (id integer);\n" for call in connection.execute.await_args_list)


@pytest.mark.asyncio
async def test_status_exposes_only_active_baseline_for_fresh_database(tmp_path, monkeypatch) -> None:
    versions_dir = tmp_path / "versions"
    _write_squash(versions_dir)
    monkeypatch.setattr(migration_runner, "_VERSIONS_DIR", versions_dir)
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])

    status = await migration_runner.get_status(pool)

    assert status == {"applied": [], "pending": ["102_squashed_baseline"]}


def test_manifest_rejects_duplicate_replacement(tmp_path, monkeypatch) -> None:
    versions_dir = tmp_path / "versions"
    versions_dir.mkdir()
    (versions_dir / "102_squashed_baseline.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (versions_dir / "102_squashed_baseline.replaces").write_text("001_first\n001_first\n", encoding="utf-8")
    monkeypatch.setattr(migration_runner, "_VERSIONS_DIR", versions_dir)

    with pytest.raises(RuntimeError, match="Duplicate migration 001_first"):
        migration_runner._discover_squashes()
