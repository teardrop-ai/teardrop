# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""http_fetch – fetch and extract content from a URL with SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
import httpx
from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── SSRF Guard ───────────────────────────────────────────────────────────────

# RFC-1918, loopback, link-local, and cloud metadata ranges to block
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC-1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC-1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC-1918
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local (metadata endpoint)
    ipaddress.ip_network("0.0.0.0/8"),  # "This" network
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 ULA (private)
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped IPv6 space
]


def _is_ip_blocked(ip_str: str) -> bool:
    """Check if an IP address falls within any blocked range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Invalid IP → block
    # Unwrap IPv4-mapped IPv6 (::ffff:x.x.x.x) so it matches IPv4 blocked ranges.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
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


def validate_url_with_ips(url: str) -> tuple[str | None, list[str]]:
    """Validate a URL for SSRF safety and return the validated resolved IPs.

    Returns ``(error, ips)``. When ``error`` is ``None`` the request may proceed
    and ``ips`` holds every address the hostname resolved to (all verified safe).
    Callers MUST connect only to these exact IPs (via ``make_ssrf_safe_connector``)
    to close the DNS-rebinding TOCTOU window between validation and connection.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL", []

    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme} (only http/https allowed)", []

    hostname = parsed.hostname
    if not hostname:
        return "No hostname in URL", []

    # Raw IP literal in the URL — validate directly, no DNS needed.
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_ip_blocked(str(addr)):
            return f"Blocked IP address: {hostname}", []
        return None, [str(addr)]
    except ValueError:
        pass  # Not a raw IP — resolve via DNS below.

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return f"DNS resolution failed for: {hostname}", []

    ips: list[str] = []
    for _family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        if _is_ip_blocked(ip_str):
            return f"Hostname {hostname} resolves to blocked IP: {ip_str}", []
        if ip_str not in ips:
            ips.append(ip_str)

    if not ips:
        return f"DNS resolution failed for: {hostname}", []
    return None, ips


async def async_validate_url_with_ips(url: str) -> tuple[str | None, list[str]]:
    """Async wrapper around ``validate_url_with_ips`` (DNS runs in a thread)."""
    return await asyncio.to_thread(validate_url_with_ips, url)


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """aiohttp resolver that pins a hostname to pre-validated IP addresses.

    The IPs were already checked against the SSRF blocklist by
    ``validate_url_with_ips``. Pinning forces the actual TCP connection to use
    exactly those addresses, so a DNS record that changes to a private/metadata
    IP between validation and connection (DNS rebinding) cannot be exploited.
    Any host other than the pinned one is refused.
    """

    def __init__(self, hostname: str, ips: list[str]) -> None:
        self._hostname = hostname
        self._ips = ips

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
        if host != self._hostname:
            raise OSError(f"SSRF guard: refusing to resolve unexpected host {host!r}")
        results: list[dict[str, Any]] = []
        for ip in self._ips:
            try:
                ip_family = socket.AF_INET6 if ipaddress.ip_address(ip).version == 6 else socket.AF_INET
            except ValueError:
                continue
            if family not in (socket.AF_UNSPEC, ip_family):
                continue
            results.append(
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": ip_family,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise OSError(f"SSRF guard: no pinned IP for host {host!r} family {family}")
        return results

    async def close(self) -> None:
        return None


def make_ssrf_safe_connector(hostname: str, ips: list[str]) -> aiohttp.TCPConnector:
    """Build an aiohttp connector that only connects to ``ips`` for ``hostname``.

    Use together with ``validate_url_with_ips`` to eliminate the DNS-rebinding
    TOCTOU window: validation and connection both target the same pinned IPs.
    """
    return aiohttp.TCPConnector(resolver=_PinnedResolver(hostname, ips))


class _SSRFGuardHTTPXTransport(httpx.AsyncHTTPTransport):
    """httpx transport that re-validates and pins every connection (incl. redirects).

    httpx resolves DNS inside httpcore at connect time, so a URL validated by
    the caller can still rebind to a private/metadata IP before the socket
    opens. This transport intercepts each outgoing request (httpx invokes it
    per redirect hop too), re-resolves the host, rejects any address on the
    SSRF blocklist, and rewrites the connection to the validated IP while
    preserving the original Host header and TLS SNI — closing the rebinding
    window for httpx-based clients (a2a, MCP).
    """

    async def handle_async_request(self, request: "httpx.Request") -> "httpx.Response":
        original_host = request.url.host
        if not original_host:
            raise httpx.ConnectError("SSRF guard: missing host", request=request)

        error, ips = await async_validate_url_with_ips(str(request.url))
        if error:
            raise httpx.ConnectError(f"SSRF guard: {error}", request=request)

        pinned_ip = ips[0]
        request.url = request.url.copy_with(host=pinned_ip)
        # Preserve virtual-host routing and certificate validation against the
        # real hostname even though we connect to the pinned IP.
        request.headers["host"] = (
            f"{original_host}:{request.url.port}" if request.url.port else original_host
        )
        request.extensions = {**request.extensions, "sni_hostname": original_host}
        return await super().handle_async_request(request)


def make_ssrf_safe_httpx_transport() -> httpx.AsyncHTTPTransport:
    """Build an httpx transport that pins connections to SSRF-validated IPs."""
    return _SSRFGuardHTTPXTransport()




# ─── Schemas ──────────────────────────────────────────────────────────────────


class HttpFetchInput(BaseModel):
    url: str = Field(..., min_length=10, max_length=2000, description="URL to fetch (http or https only)")
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

_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB
_REQUEST_TIMEOUT = 10  # seconds
_USER_AGENT = "Teardrop/1.0 (AI Agent; +https://teardrop.dev)"
_MAX_REDIRECTS = 5  # hop cap for the SSRF-validated redirect loop


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
    # SSRF validation (async DNS to avoid blocking event loop). We pin the
    # connection to the exact IPs we just validated to close the DNS-rebinding
    # TOCTOU window between this check and the TCP connect.
    error, pinned_ips = await async_validate_url_with_ips(url)
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
        # Follow redirects manually so every hop is re-validated against the
        # SSRF guard. aiohttp's built-in redirect handling would resolve a
        # public URL that 3xx-redirects to an internal/metadata target before
        # we ever see it, defeating async_validate_url.
        current_url = url
        current_ips = pinned_ips
        for _hop in range(_MAX_REDIRECTS + 1):
            current_host = urlparse(current_url).hostname or ""
            connector = make_ssrf_safe_connector(current_host, current_ips)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    current_url,
                    timeout=timeout,
                    headers={"User-Agent": _USER_AGENT},
                    max_field_size=8190,
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            return {
                                "url": url,
                                "title": None,
                                "content": "Redirect missing Location header",
                                "content_length": 0,
                                "truncated": False,
                            }
                        # Resolve relative redirects against the current URL.
                        next_url = urljoin(current_url, location)
                        redirect_error, next_ips = await async_validate_url_with_ips(next_url)
                        if redirect_error:
                            return {
                                "url": url,
                                "title": None,
                                "content": f"Redirect blocked: {redirect_error}",
                                "content_length": 0,
                                "truncated": False,
                            }
                        current_url = next_url
                        current_ips = next_ips
                        continue

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
                    break
        else:
            return {
                "url": url,
                "title": None,
                "content": "Too many redirects",
                "content_length": 0,
                "truncated": False,
            }

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
