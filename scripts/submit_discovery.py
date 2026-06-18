#!/usr/bin/env python3
"""Validate Teardrop discovery surfaces and optionally submit them to registries."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


def _ensure_repo_root_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


_ensure_repo_root_on_path()

import httpx  # noqa: E402

from teardrop.a2a_client import async_validate_url  # noqa: E402
from tools.definitions.http_fetch import make_ssrf_safe_httpx_transport  # noqa: E402

_KNOWN_MANUAL_TARGETS = {
    "Smithery": "https://smithery.ai/",
    "llmstxt.site": "https://llmstxt.site/",
    "directory.llmstxt.cloud": "https://directory.llmstxt.cloud/",
}


def _default_base_url() -> str:
    from teardrop.config import get_settings

    settings = get_settings()
    if settings.app_base_url:
        return settings.app_base_url.rstrip("/")
    return f"http://{settings.app_host}:{settings.app_port}".rstrip("/")


def _normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def validate_llms_txt(content: str) -> list[str]:
    errors: list[str] = []
    lines = content.splitlines()
    if not lines or not lines[0].startswith("# "):
        errors.append("llms.txt must begin with an H1 title line.")
    if "[" not in content or "](" not in content:
        errors.append("llms.txt should include at least one markdown link.")
    if not re.search(r"\[[^\]]+\]\(https?://[^)]+\)", content):
        errors.append("llms.txt should contain at least one absolute http/https markdown link.")
    return errors


def validate_agent_card(card: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required_fields = [
        "name",
        "description",
        "version",
        "url",
        "capabilities",
        "skills",
        "supportedInterfaces",
        "defaultInputModes",
        "defaultOutputModes",
        "securitySchemes",
    ]
    for field_name in required_fields:
        if field_name not in card:
            errors.append(f"agent-card missing required field: {field_name}")

    if isinstance(card.get("supportedInterfaces"), list) and not card["supportedInterfaces"]:
        errors.append("agent-card supportedInterfaces must not be empty.")

    for index, interface in enumerate(card.get("supportedInterfaces", [])):
        for field_name in ("url", "protocolBinding", "protocolVersion"):
            if field_name not in interface:
                errors.append(f"agent-card supportedInterfaces[{index}] missing {field_name}")

    for index, skill in enumerate(card.get("skills", [])):
        if "id" not in skill:
            errors.append(f"agent-card skills[{index}] missing id")

    schemes = card.get("securitySchemes")
    if schemes and not isinstance(schemes, dict):
        errors.append("agent-card securitySchemes must be an object.")

    url = card.get("url", "")
    if url and not url.startswith(("http://", "https://")):
        errors.append("agent-card url must be absolute http/https.")

    return errors


async def validate_registry_urls(registry_urls: Sequence[str]) -> list[str]:
    safe_urls: list[str] = []
    for registry_url in registry_urls:
        url = registry_url.strip()
        if not url:
            continue
        error = await async_validate_url(url)
        if error:
            raise ValueError(f"Registry URL rejected by SSRF validation: {url} ({error})")
        safe_urls.append(url.rstrip("/"))
    return safe_urls


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    return response.text


def build_submission_payload(
    *,
    base_url: str,
    agent_card: dict[str, Any],
    mcp_server_card: dict[str, Any],
) -> dict[str, Any]:
    return {
        "name": agent_card.get("name", "Teardrop"),
        "base_url": base_url,
        "agent_card_url": f"{base_url}/.well-known/agent-card.json",
        "legacy_agent_card_url": f"{base_url}/.well-known/agent.json",
        "mcp_server_card_url": f"{base_url}/.well-known/mcp/server-card.json",
        "llms_url": f"{base_url}/llms.txt",
        "agent_card": agent_card,
        "mcp_server_card": mcp_server_card,
    }


async def submit_payloads(
    *,
    client: httpx.AsyncClient,
    registry_urls: Sequence[str],
    payload: dict[str, Any],
    dry_run: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for registry_url in registry_urls:
        if dry_run:
            results.append({"url": registry_url, "submitted": False, "dry_run": True})
            continue
        response = await client.post(registry_url, json=payload)
        response.raise_for_status()
        results.append({"url": registry_url, "submitted": True, "status_code": response.status_code})
    return results


async def run_validation(
    *,
    base_url: str,
    registry_urls: Sequence[str],
    timeout: float,
    dry_run: bool,
) -> int:
    safe_registry_urls = await validate_registry_urls(registry_urls)
    agent_card_url = f"{base_url}/.well-known/agent-card.json"
    mcp_server_card_url = f"{base_url}/.well-known/mcp/server-card.json"
    llms_url = f"{base_url}/llms.txt"

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        transport=make_ssrf_safe_httpx_transport(),
    ) as client:
        agent_card = await _fetch_json(client, agent_card_url)
        mcp_server_card = await _fetch_json(client, mcp_server_card_url)
        llms_txt = await _fetch_text(client, llms_url)

        errors = [
            *validate_agent_card(agent_card),
            *validate_llms_txt(llms_txt),
        ]
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1

        payload = build_submission_payload(
            base_url=base_url,
            agent_card=agent_card,
            mcp_server_card=mcp_server_card,
        )
        results = await submit_payloads(
            client=client,
            registry_urls=safe_registry_urls,
            payload=payload,
            dry_run=dry_run,
        )

    print(json.dumps({"validated": True, "base_url": base_url, "submissions": results}, indent=2))
    print("Manual discovery directories:")
    for name, url in _KNOWN_MANUAL_TARGETS.items():
        print(f"- {name}: {url}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="", help="Base URL to validate, e.g. https://api.teardrop.dev")
    parser.add_argument(
        "--registry-url",
        action="append",
        default=[],
        help="Registry submission endpoint. May be provided multiple times.",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--submit",
        action="store_true",
        help="POST the discovery payload to registry URLs instead of dry-run validation only.",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    base_url = _normalize_base_url(args.base_url or _default_base_url())

    registry_urls = list(args.registry_url)
    if not registry_urls:
        from teardrop.config import get_settings

        registry_urls = list(get_settings().discovery_registry_urls)

    return asyncio.run(
        run_validation(
            base_url=base_url,
            registry_urls=registry_urls,
            timeout=args.timeout,
            dry_run=not args.submit,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
