"""Audit Python dependencies for upgrade drift and published OSV vulnerabilities."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_MANIFESTS = ("requirements.txt", "requirements-dev.txt", "pyproject.toml")
PACKAGE_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
USER_AGENT = "teardrop/dependency-audit"
REQUEST_TIMEOUT_SECONDS = 10
HIGH_OR_CRITICAL = {"HIGH", "CRITICAL"}
CHANGELOG_KEYS = (
    "changelog",
    "changes",
    "release notes",
    "release-notes",
    "releases",
    "news",
    "history",
)


class AuditError(RuntimeError):
    """Raised when the audit cannot produce a trustworthy result."""


def normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement_name(raw_line: str) -> str | None:
    line = raw_line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    match = PACKAGE_NAME_RE.match(line)
    if match is None:
        return None
    return match.group(1)


def merge_dependency(targets: dict[str, dict[str, Any]], package_name: str, source: str) -> None:
    normalized = normalize_package_name(package_name)
    entry = targets.setdefault(
        normalized,
        {
            "name": package_name,
            "normalized_name": normalized,
            "sources": [],
        },
    )
    if source not in entry["sources"]:
        entry["sources"].append(source)


def load_requirements_manifest(
    manifest_path: Path,
    targets: dict[str, dict[str, Any]],
    seen: set[Path],
) -> None:
    resolved = manifest_path.resolve()
    if resolved in seen or not manifest_path.exists():
        return
    seen.add(resolved)

    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            _, nested = line.split(maxsplit=1)
            load_requirements_manifest((manifest_path.parent / nested).resolve(), targets, seen)
            continue
        package_name = parse_requirement_name(line)
        if package_name is not None:
            merge_dependency(targets, package_name, manifest_path.name)


def load_pyproject_manifest(manifest_path: Path, targets: dict[str, dict[str, Any]]) -> None:
    if not manifest_path.exists():
        return

    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    for requirement in project.get("dependencies", []):
        package_name = parse_requirement_name(requirement)
        if package_name is not None:
            merge_dependency(targets, package_name, manifest_path.name)

    for dependency_group, requirements in project.get("optional-dependencies", {}).items():
        for requirement in requirements:
            package_name = parse_requirement_name(requirement)
            if package_name is not None:
                merge_dependency(targets, package_name, f"{manifest_path.name}:{dependency_group}")


def load_dependency_targets(manifest_paths: Iterable[Path]) -> list[dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    seen_requirements: set[Path] = set()

    for manifest_path in manifest_paths:
        if manifest_path.name.endswith(".txt"):
            load_requirements_manifest(manifest_path, targets, seen_requirements)
        elif manifest_path.name == "pyproject.toml":
            load_pyproject_manifest(manifest_path, targets)

    return sorted(targets.values(), key=lambda item: item["name"].lower())


def installed_distributions() -> dict[str, dict[str, str]]:
    installed: dict[str, dict[str, str]] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        installed[normalize_package_name(name)] = {
            "distribution": name,
            "version": distribution.version,
        }
    return installed


def request_json(url: str, *, data: bytes | None = None, timeout: int = REQUEST_TIMEOUT_SECONDS) -> Any:
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)

    last_error: Exception | None = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            last_error = exc
    raise AuditError(f"Request failed for {url}: {last_error}")


def select_changelog_url(project_urls: dict[str, str] | None) -> str | None:
    if not project_urls:
        return None

    lowered = {key.lower(): value for key, value in project_urls.items() if value}
    for key in CHANGELOG_KEYS:
        if key in lowered:
            return lowered[key]

    for key, value in project_urls.items():
        if key and value and "release" in key.lower():
            return value
    return None


def fetch_pypi_metadata(package_name: str) -> dict[str, str | None]:
    safe_name = urllib.parse.quote(package_name, safe="")
    payload = request_json(f"https://pypi.org/pypi/{safe_name}/json")
    info = payload.get("info", {})
    return {
        "latest": info.get("version"),
        "latest_changelog_url": select_changelog_url(info.get("project_urls")) or info.get("project_url"),
    }


def fetch_osv_vulnerability(vulnerability_id: str) -> dict[str, Any]:
    safe_id = urllib.parse.quote(vulnerability_id, safe="")
    payload = request_json(f"https://api.osv.dev/v1/vulns/{safe_id}")
    if not isinstance(payload, dict):
        raise AuditError(f"OSV detail response for {vulnerability_id} was not an object.")
    return payload


def severity_from_score(score: str | None) -> str:
    if not score:
        return "UNKNOWN"
    try:
        numeric = float(score)
    except ValueError:
        return "UNKNOWN"

    if numeric >= 9.0:
        return "CRITICAL"
    if numeric >= 7.0:
        return "HIGH"
    if numeric >= 4.0:
        return "MEDIUM"
    if numeric > 0:
        return "LOW"
    return "UNKNOWN"


def extract_severity(vulnerability: dict[str, Any]) -> str:
    database_severity = vulnerability.get("database_specific", {}).get("severity")
    if isinstance(database_severity, str) and database_severity:
        return database_severity.upper()

    severities = vulnerability.get("severity", [])
    if isinstance(severities, list):
        for severity in severities:
            if not isinstance(severity, dict):
                continue
            mapped = severity_from_score(severity.get("score"))
            if mapped != "UNKNOWN":
                return mapped

    for affected in vulnerability.get("affected", []):
        affected_severity = affected.get("database_specific", {}).get("severity")
        if isinstance(affected_severity, str) and affected_severity:
            return affected_severity.upper()
    return "UNKNOWN"


def extract_fixed_versions(vulnerability: dict[str, Any]) -> list[str]:
    fixed_versions: list[str] = []
    for affected in vulnerability.get("affected", []):
        for range_block in affected.get("ranges", []):
            for event in range_block.get("events", []):
                fixed_version = event.get("fixed")
                if isinstance(fixed_version, str) and fixed_version not in fixed_versions:
                    fixed_versions.append(fixed_version)
    return fixed_versions


def compact_vulnerability(vulnerability: dict[str, Any]) -> dict[str, Any]:
    aliases = [alias for alias in vulnerability.get("aliases", []) if isinstance(alias, str)]
    references = vulnerability.get("references", [])
    reference_urls = [reference.get("url") for reference in references if isinstance(reference, dict) and reference.get("url")]
    return {
        "id": vulnerability.get("id"),
        "summary": vulnerability.get("summary") or vulnerability.get("details") or "No summary provided.",
        "severity": extract_severity(vulnerability),
        "aliases": aliases,
        "fixed_versions": extract_fixed_versions(vulnerability),
        "reference_urls": reference_urls,
    }


def query_osv_batch(packages: Sequence[dict[str, str]]) -> list[list[dict[str, Any]]]:
    if not packages:
        return []

    payload = {
        "queries": [
            {
                "package": {"name": package["name"], "ecosystem": "PyPI"},
                "version": package["version"],
            }
            for package in packages
        ]
    }
    response = request_json("https://api.osv.dev/v1/querybatch", data=json.dumps(payload).encode("utf-8"))
    results = response.get("results")
    if not isinstance(results, list):
        raise AuditError("OSV batch response did not contain a results list.")

    vulnerability_cache: dict[str, dict[str, Any]] = {}
    mapped_results: list[list[dict[str, Any]]] = []
    for result in results:
        vulns = result.get("vulns", []) if isinstance(result, dict) else []
        mapped_results.append(
            [
                compact_vulnerability(
                    vulnerability_cache.setdefault(vulnerability["id"], fetch_osv_vulnerability(vulnerability["id"]))
                    if isinstance(vulnerability, dict) and vulnerability.get("id")
                    else vulnerability
                )
                for vulnerability in vulns
                if isinstance(vulnerability, dict)
            ]
        )
    return mapped_results


def build_report(manifest_paths: Sequence[Path]) -> dict[str, Any]:
    targets = load_dependency_targets(manifest_paths)
    if not targets:
        raise AuditError("No dependency manifests were found or they did not contain supported dependencies.")

    installed = installed_distributions()
    osv_requests: list[dict[str, str]] = []
    osv_indexes: list[int] = []
    packages: list[dict[str, Any]] = []

    for target in targets:
        distribution = installed.get(target["normalized_name"], {})
        installed_version = distribution.get("version")
        metadata_error: str | None = None
        latest_version: str | None = None
        latest_changelog_url: str | None = None
        try:
            metadata = fetch_pypi_metadata(target["name"])
            latest_version = metadata["latest"]
            latest_changelog_url = metadata["latest_changelog_url"]
        except AuditError as exc:
            metadata_error = str(exc)

        package_entry = {
            "name": target["name"],
            "installed": installed_version,
            "distribution": distribution.get("distribution"),
            "latest": latest_version,
            "upgrade_available": bool(installed_version and latest_version and installed_version != latest_version),
            "latest_changelog_url": latest_changelog_url,
            "sources": target["sources"],
            "metadata_error": metadata_error,
            "vulnerabilities": [],
        }
        if installed_version:
            osv_indexes.append(len(packages))
            osv_requests.append({"name": target["name"], "version": installed_version})
        packages.append(package_entry)

    for package_index, vulnerabilities in zip(osv_indexes, query_osv_batch(osv_requests), strict=True):
        packages[package_index]["vulnerabilities"] = vulnerabilities

    vulnerable_packages = 0
    high_or_critical = 0
    upgrade_available = 0
    for package in packages:
        if package["upgrade_available"]:
            upgrade_available += 1
        if package["vulnerabilities"]:
            vulnerable_packages += 1
            if any(vulnerability["severity"] in HIGH_OR_CRITICAL for vulnerability in package["vulnerabilities"]):
                high_or_critical += 1

    return {
        "manifests": [str(path) for path in manifest_paths if path.exists()],
        "summary": {
            "total_packages": len(packages),
            "upgrade_available": upgrade_available,
            "vulnerable_packages": vulnerable_packages,
            "high_or_critical_packages": high_or_critical,
        },
        "packages": packages,
    }


def report_has_high_or_critical(report: dict[str, Any]) -> bool:
    return any(
        vulnerability["severity"] in HIGH_OR_CRITICAL
        for package in report["packages"]
        for vulnerability in package["vulnerabilities"]
    )


def print_text_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(
        "Dependency audit: "
        f"{summary['total_packages']} packages, "
        f"{summary['upgrade_available']} with newer releases, "
        f"{summary['vulnerable_packages']} with vulnerabilities, "
        f"{summary['high_or_critical_packages']} high/critical."
    )

    for package in report["packages"]:
        installed = package["installed"] or "not installed"
        latest = package["latest"] or "unknown"
        print(f"- {package['name']}: installed={installed} latest={latest} sources={','.join(package['sources'])}")
        if package["metadata_error"]:
            print(f"  metadata_error: {package['metadata_error']}")
        if not package["vulnerabilities"]:
            continue
        for vulnerability in package["vulnerabilities"]:
            fixed_versions = ",".join(vulnerability["fixed_versions"]) or "unknown"
            print(
                f"  {vulnerability['severity']} {vulnerability['id']} fixed={fixed_versions} summary={vulnerability['summary']}"
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        dest="manifests",
        action="append",
        default=[],
        help="Path to a dependency manifest. Defaults to requirements.txt, requirements-dev.txt, and pyproject.toml.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the full report as JSON.")
    parser.add_argument("--txt", action="store_true", help="Emit a human-readable text summary.")
    parser.add_argument(
        "--fail-on-critical",
        action="store_true",
        help="Exit with status 1 when a HIGH or CRITICAL vulnerability is present.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest_paths = [Path(path) for path in args.manifests] if args.manifests else [Path(path) for path in DEFAULT_MANIFESTS]
    emit_json = args.json or not args.txt

    try:
        report = build_report(manifest_paths)
    except AuditError as exc:
        print(f"dependency audit failed: {exc}", file=sys.stderr)
        return 2

    if emit_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    if args.txt:
        print_text_report(report)

    if args.fail_on_critical and report_has_high_or_critical(report):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
