# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""http_fetch – fetch and extract content from a URL with SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import aiohttp
from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── SSRF Guard ───────────────────────────────────────────────────────────────

# RFC-1918, loopback, link-local, and cloud metadata ranges to block
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC-1918
    ipaddress.ip_network("172.16.0.0/12"),     # RFC-1918
    ipaddress.ip_network("192.168.0.0/16"),    # RFC-1918
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local (metadata endpoint)
    ipaddress.ip_network("0.0.0.0/8"),         # "This" network
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA (private)
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def _is_ip_blocked(ip_str: str) -> bool:
    """Check if an IP address falls within any blocked range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Invalid IP → block
    return any(addr in net for net in _BLOCKED_NETWORKS)


def validate_url(url: str) -> str | None:
    """Validate a URL for SSRF safety (sync, blocking DNS). Returns error or None if safe.

    WARNING: Calls socket.getaddrinfo() which blocks the thread.  Hot-path async
    callers should use ``async_validate_url`` instead.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"

    # Scheme check
    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme} (only http/https allowed)"

    # Hostname required
    hostname = parsed.hostname
    if not hostname:
        return "No hostname in URL"

    # Block numeric IP addresses in URL directly
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_ip_blocked(str(addr)):
            return f"Blocked IP address: {hostname}"
    except ValueError:
        pass  # Not a raw IP — resolve via DNS below

    # DNS resolution check (anti-rebinding)
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            if _is_ip_blocked(ip_str):
                return f"Hostname {hostname} resolves to blocked IP: {ip_str}"
    except socket.gaierror:
        return f"DNS resolution failed for: {hostname}"

    return None  # Safe


async def async_validate_url(url: str) -> str | None:
    """Async wrapper around validate_url — runs DNS in a thread pool executor.

    Use this from async request handlers to avoid blocking the event loop
    during DNS resolution (can take 100ms–2s per call).
    """
    return await asyncio.to_thread(validate_url, url)


# ─── Schemas ──────────────────────────────────────────────────────────────────


class HttpFetchInput(BaseModel):
    url: str = Field(
        ..., min_length=10, max_length=2000, description="URL to fetch (http or https only)"
    )
    max_chars: int = Field(
        default=8000,
        ge=100,
        le=50000,
        description="Maximum characters of extracted content to return",
    )


class HttpFetchOutput(BaseModel):
    url: str
    title: str | None
    content: str
    content_length: int
    truncated: bool


# ─── Implementation ──────────────────────────────────────────────────────────

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB
_REQUEST_TIMEOUT = 10  # seconds
_USER_AGENT = "Teardrop/1.0 (AI Agent; +https://teardrop.dev)"


def _extract_content(html: str) -> tuple[str | None, str]:
    """Extract main text content from HTML. Returns (title, content)."""
    try:
        import trafilatura

        result = trafilatura.extract(html, include_comments=False, include_tables=True)
        if result:
            # Try to get title
            metadata = trafilatura.extract_metadata(html)
            title = metadata.title if metadata else None
            return title, result
    except ImportError:
        logger.debug("trafilatura not installed; falling back to basic extraction")
    except Exception as exc:
        logger.debug("trafilatura extraction failed: %s", exc)

    # Basic fallback: strip tags
    import re

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else None
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return title, text


async def http_fetch(url: str, max_chars: int = 8000) -> dict[str, Any]:
    """Fetch a URL and return extracted text content."""
    # SSRF validation
    error = validate_url(url)
    if error:
        return {
            "url": url,
            "title": None,
            "content": "",
            "content_length": 0,
            "truncated": False,
            "error": error,
        }

    try:
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=timeout,
                headers={"User-Agent": _USER_AGENT},
                max_field_size=8190,
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return {
                        "url": url,
                        "title": None,
                        "content": f"HTTP {resp.status}",
                        "content_length": 0,
                        "truncated": False,
                    }

                # Enforce size limit
                content_length = resp.content_length
                if content_length and content_length > _MAX_RESPONSE_BYTES:
                    return {
                        "url": url,
                        "title": None,
                        "content": f"Response too large: {content_length} bytes",
                        "content_length": 0,
                        "truncated": False,
                    }

                body = await resp.read()
                if len(body) > _MAX_RESPONSE_BYTES:
                    body = body[:_MAX_RESPONSE_BYTES]

                # Detect encoding
                encoding = resp.charset or "utf-8"
                try:
                    html = body.decode(encoding, errors="replace")
                except (UnicodeDecodeError, LookupError):
                    html = body.decode("utf-8", errors="replace")

    except aiohttp.ClientError as exc:
        return {
            "url": url,
            "title": None,
            "content": f"Fetch error: {type(exc).__name__}",
            "content_length": 0,
            "truncated": False,
        }
    except Exception as exc:
        return {
            "url": url,
            "title": None,
            "content": f"Unexpected error: {type(exc).__name__}",
            "content_length": 0,
            "truncated": False,
        }

    title, content = _extract_content(html)

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]

    return {
        "url": url,
        "title": title,
        "content": content,
        "content_length": len(content),
        "truncated": truncated,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="http_fetch",
    version="1.0.0",
    description=(
        "Fetch a web page and extract its main text content. Useful for reading "
        "articles, documentation, and web resources. Returns cleaned text, not raw HTML."
    ),
    tags=["web", "http", "fetch", "content"],
    input_schema=HttpFetchInput,
    output_schema=HttpFetchOutput,
    implementation=http_fetch,
)
