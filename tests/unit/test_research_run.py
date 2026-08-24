# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.research_run import REPO_ROOT, main


def test_main_loads_dotenv_and_delegates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RESEARCH_TEST_KEY=loaded-value\n", encoding="utf-8")
    monkeypatch.setattr("scripts.research_run.REPO_ROOT", tmp_path)

    captured: list[list[str] | None] = []

    def fake_research_main(argv: list[str] | None) -> int:
        captured.append(argv)
        return 0

    monkeypatch.setattr("scripts.research_run.research_main", fake_research_main)

    result = main(["--topic", "security", "--query", "Audit billing"])

    assert result == 0
    assert captured == [["--topic", "security", "--query", "Audit billing"]]
    assert os.environ.get("RESEARCH_TEST_KEY") == "loaded-value"


def test_main_does_not_override_existing_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RESEARCH_TEST_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setattr("scripts.research_run.REPO_ROOT", tmp_path)
    monkeypatch.setenv("RESEARCH_TEST_KEY", "from-env")

    def fake_research_main(argv: list[str] | None) -> int:
        return 0

    monkeypatch.setattr("scripts.research_run.research_main", fake_research_main)

    main(["--topic", "security", "--query", "Audit billing"])

    assert os.environ.get("RESEARCH_TEST_KEY") == "from-env"


def test_repo_root_points_at_repository() -> None:
    assert (REPO_ROOT / "scripts" / "research_repo.py").is_file()
