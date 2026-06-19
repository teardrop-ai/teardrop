from __future__ import annotations

import textwrap

import pytest

import scripts.audit_dependencies as audit


def write_file(path, content: str):
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
    return path


def test_load_dependency_targets_reads_requirements_and_pyproject(tmp_path):
    requirements = write_file(
        tmp_path / "requirements.txt",
        """
        fastapi>=0.124.4
        x402[fastapi,evm]>=2.8.0,<3.0.0
        """,
    )
    requirements_dev = write_file(
        tmp_path / "requirements-dev.txt",
        """
        -r requirements.txt
        pytest>=8.0
        """,
    )
    pyproject = write_file(
        tmp_path / "pyproject.toml",
        """
        [project]
        dependencies = ["httpx>=0.28.0"]

        [project.optional-dependencies]
        docs = ["mkdocs>=1.6.0"]
        """,
    )

    targets = audit.load_dependency_targets([requirements, requirements_dev, pyproject])
    targets_by_name = {item["normalized_name"]: item for item in targets}

    assert targets_by_name["fastapi"]["sources"] == ["requirements.txt"]
    assert targets_by_name["pytest"]["sources"] == ["requirements-dev.txt"]
    assert targets_by_name["httpx"]["sources"] == ["pyproject.toml"]
    assert targets_by_name["mkdocs"]["sources"] == ["pyproject.toml:docs"]


def test_build_report_collects_upgrades_and_vulnerabilities(tmp_path, monkeypatch):
    requirements = write_file(
        tmp_path / "requirements.txt",
        """
        fastapi>=0.124.4
        x402[fastapi,evm]>=2.8.0,<3.0.0
        """,
    )

    monkeypatch.setattr(
        audit,
        "installed_distributions",
        lambda: {
            "fastapi": {"distribution": "fastapi", "version": "0.124.4"},
            "x402": {"distribution": "x402", "version": "2.8.0"},
        },
    )
    monkeypatch.setattr(
        audit,
        "fetch_pypi_metadata",
        lambda name: {
            "latest": {"fastapi": "0.124.4", "x402": "2.8.3"}[name],
            "latest_changelog_url": f"https://example.com/{name}/releases",
        },
    )
    monkeypatch.setattr(
        audit,
        "query_osv_batch",
        lambda packages: [
            [],
            [
                {
                    "id": "OSV-2026-1",
                    "summary": "Permit validation bypass.",
                    "severity": "HIGH",
                    "aliases": ["CVE-2026-0001"],
                    "fixed_versions": ["2.8.3"],
                    "reference_urls": ["https://osv.dev/OSV-2026-1"],
                }
            ],
        ],
    )

    report = audit.build_report([requirements])
    packages_by_name = {package["name"]: package for package in report["packages"]}

    assert report["summary"] == {
        "total_packages": 2,
        "upgrade_available": 1,
        "vulnerable_packages": 1,
        "high_or_critical_packages": 1,
    }
    assert packages_by_name["x402"]["upgrade_available"] is True
    assert packages_by_name["x402"]["vulnerabilities"][0]["severity"] == "HIGH"
    assert audit.report_has_high_or_critical(report) is True


def test_build_report_preserves_metadata_errors(tmp_path, monkeypatch):
    requirements = write_file(tmp_path / "requirements.txt", "fastapi>=0.124.4")

    monkeypatch.setattr(
        audit,
        "installed_distributions",
        lambda: {"fastapi": {"distribution": "fastapi", "version": "0.124.4"}},
    )

    def fail_metadata(_name: str):
        raise audit.AuditError("PyPI unavailable")

    monkeypatch.setattr(audit, "fetch_pypi_metadata", fail_metadata)
    monkeypatch.setattr(audit, "query_osv_batch", lambda packages: [[]])

    report = audit.build_report([requirements])

    assert report["packages"][0]["metadata_error"] == "PyPI unavailable"
    assert report["packages"][0]["latest"] is None


def test_query_osv_batch_rejects_malformed_response(monkeypatch):
    monkeypatch.setattr(audit, "request_json", lambda url, data=None, timeout=10: {"unexpected": []})

    with pytest.raises(audit.AuditError, match="results list"):
        audit.query_osv_batch([{"name": "fastapi", "version": "0.124.4"}])


def test_query_osv_batch_hydrates_vulnerability_details(monkeypatch):
    def fake_request_json(url, data=None, timeout=10):
        if url == "https://api.osv.dev/v1/querybatch":
            return {"results": [{"vulns": [{"id": "GHSA-5rvq-cxj2-64vf"}]}]}
        if url == "https://api.osv.dev/v1/vulns/GHSA-5rvq-cxj2-64vf":
            return {
                "id": "GHSA-5rvq-cxj2-64vf",
                "summary": "Multipart header parsing issue.",
                "database_specific": {"severity": "HIGH"},
                "affected": [
                    {
                        "ranges": [
                            {
                                "events": [
                                    {"introduced": "0"},
                                    {"fixed": "0.0.30"},
                                ]
                            }
                        ]
                    }
                ],
                "references": [{"url": "https://github.com/advisories/GHSA-5rvq-cxj2-64vf"}],
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(audit, "request_json", fake_request_json)

    results = audit.query_osv_batch([{"name": "python-multipart", "version": "0.0.22"}])

    assert results == [
        [
            {
                "id": "GHSA-5rvq-cxj2-64vf",
                "summary": "Multipart header parsing issue.",
                "severity": "HIGH",
                "aliases": [],
                "fixed_versions": ["0.0.30"],
                "reference_urls": ["https://github.com/advisories/GHSA-5rvq-cxj2-64vf"],
            }
        ]
    ]


def test_main_returns_expected_exit_codes(monkeypatch, capsys):
    high_report = {
        "summary": {
            "total_packages": 1,
            "upgrade_available": 0,
            "vulnerable_packages": 1,
            "high_or_critical_packages": 1,
        },
        "packages": [
            {
                "name": "x402",
                "installed": "2.8.0",
                "distribution": "x402",
                "latest": "2.8.3",
                "upgrade_available": True,
                "latest_changelog_url": "https://example.com/x402/releases",
                "sources": ["requirements.txt"],
                "metadata_error": None,
                "vulnerabilities": [
                    {
                        "id": "OSV-2026-1",
                        "summary": "Permit validation bypass.",
                        "severity": "HIGH",
                        "aliases": [],
                        "fixed_versions": ["2.8.3"],
                        "reference_urls": [],
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(audit, "build_report", lambda manifests, ignore_advisories=None: high_report)

    assert audit.main(["--txt", "--fail-on-critical"]) == 1
    assert "Dependency audit: 1 packages" in capsys.readouterr().out

    def fail_build(_manifests, ignore_advisories=None):
        raise audit.AuditError("broken")

    monkeypatch.setattr(audit, "build_report", fail_build)

    assert audit.main(["--json"]) == 2
    assert "dependency audit failed: broken" in capsys.readouterr().err


def test_main_with_ignore_advisory_bypasses_failure(monkeypatch, capsys):
    high_report_template = {
        "packages": [
            {
                "name": "x402",
                "installed": "2.8.0",
                "distribution": "x402",
                "latest": "2.8.3",
                "upgrade_available": True,
                "latest_changelog_url": "https://example.com/x402/releases",
                "sources": ["requirements.txt"],
                "metadata_error": None,
                "vulnerabilities": [
                    {
                        "id": "OSV-2026-1",
                        "summary": "Permit validation bypass.",
                        "severity": "HIGH",
                        "aliases": [],
                        "fixed_versions": ["2.8.3"],
                        "reference_urls": [],
                    }
                ],
            }
        ],
    }

    # Simulate dynamic behavior of build_report which filters out high_or_critical counts if ignored
    def fake_build_report(manifests, ignore_advisories=None):
        ignored = set(ignore_advisories or [])
        high_cnt = 0 if "OSV-2026-1" in ignored else 1
        return {
            "summary": {
                "total_packages": 1,
                "upgrade_available": 1,
                "vulnerable_packages": 1,
                "high_or_critical_packages": high_cnt,
            },
            **high_report_template,
        }

    monkeypatch.setattr(audit, "build_report", fake_build_report)

    # When ignored, exit status should be 0 because we ignore the HIGH advisory
    assert audit.main(["--txt", "--fail-on-critical", "--ignore-advisory", "OSV-2026-1"]) == 0
    out = capsys.readouterr().out
    assert "OSV-2026-1 [IGNORED]" in out
    assert "0 high/critical." in out
