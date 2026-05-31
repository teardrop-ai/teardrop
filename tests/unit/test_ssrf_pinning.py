"""Unit tests for SSRF connection-pinning helpers in ``tools.definitions.http_fetch``.

These close the DNS-rebinding TOCTOU window: a hostname validated as safe could
re-resolve to a private/metadata IP before the socket connects. ``validate_url_with_ips``
returns the exact validated IPs, and the pinned aiohttp/httpx transports force the
connection to use only those IPs.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tools.definitions import http_fetch

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _addrinfo(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ips]


# ─── validate_url_with_ips ───────────────────────────────────────────────────


def test_validate_blocks_bad_scheme():
    error, ips = http_fetch.validate_url_with_ips("ftp://example.com/x")
    assert error is not None
    assert ips == []


def test_validate_blocks_raw_private_ip():
    error, ips = http_fetch.validate_url_with_ips("http://169.254.169.254/latest/meta-data")
    assert error is not None
    assert "Blocked IP" in error
    assert ips == []


def test_validate_allows_raw_public_ip():
    error, ips = http_fetch.validate_url_with_ips("https://93.184.216.34/")
    assert error is None
    assert ips == ["93.184.216.34"]


def test_validate_returns_resolved_public_ips():
    with patch.object(http_fetch.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34", "93.184.216.35")):
        error, ips = http_fetch.validate_url_with_ips("https://example.com/")
    assert error is None
    assert ips == ["93.184.216.34", "93.184.216.35"]


def test_validate_blocks_when_any_ip_private():
    with patch.object(http_fetch.socket, "getaddrinfo", return_value=_addrinfo("93.184.216.34", "10.0.0.5")):
        error, ips = http_fetch.validate_url_with_ips("https://rebind.example.com/")
    assert error is not None
    assert "blocked IP" in error
    assert ips == []


def test_validate_dns_failure():
    with patch.object(http_fetch.socket, "getaddrinfo", side_effect=socket.gaierror):
        error, ips = http_fetch.validate_url_with_ips("https://nope.example.com/")
    assert error is not None
    assert ips == []


# ─── _PinnedResolver ─────────────────────────────────────────────────────────


async def test_pinned_resolver_returns_pinned_ips():
    resolver = http_fetch._PinnedResolver("example.com", ["93.184.216.34"])
    results = await resolver.resolve("example.com", 443, socket.AF_INET)
    assert len(results) == 1
    assert results[0]["host"] == "93.184.216.34"
    assert results[0]["hostname"] == "example.com"


async def test_pinned_resolver_rejects_other_host():
    resolver = http_fetch._PinnedResolver("example.com", ["93.184.216.34"])
    with pytest.raises(OSError):
        await resolver.resolve("evil.example.com", 443, socket.AF_INET)


async def test_pinned_resolver_filters_family():
    resolver = http_fetch._PinnedResolver("example.com", ["93.184.216.34"])
    # Requesting IPv6 for an IPv4-only pin yields no addresses → OSError.
    with pytest.raises(OSError):
        await resolver.resolve("example.com", 443, socket.AF_INET6)


def test_make_ssrf_safe_connector_type():
    import asyncio

    import aiohttp

    async def _build():
        return http_fetch.make_ssrf_safe_connector("example.com", ["93.184.216.34"])

    connector = asyncio.run(_build())
    assert isinstance(connector, aiohttp.TCPConnector)
    assert isinstance(connector._resolver, http_fetch._PinnedResolver)


# ─── httpx pinned transport ──────────────────────────────────────────────────


async def test_httpx_transport_blocks_rebinding():
    transport = http_fetch.make_ssrf_safe_httpx_transport()
    request = httpx.Request("GET", "https://rebind.example.com/")
    with patch.object(
        http_fetch,
        "async_validate_url_with_ips",
        new=AsyncMock(return_value=("Hostname resolves to blocked IP: 10.0.0.5", [])),
    ):
        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(request)


async def test_httpx_transport_pins_validated_ip():
    transport = http_fetch.make_ssrf_safe_httpx_transport()
    request = httpx.Request("GET", "https://example.com/path")
    with (
        patch.object(
            http_fetch,
            "async_validate_url_with_ips",
            new=AsyncMock(return_value=(None, ["93.184.216.34"])),
        ),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", new=AsyncMock(return_value="resp")),
    ):
        result = await transport.handle_async_request(request)
    assert result == "resp"
    # Connection rewritten to the pinned IP, but Host + SNI preserve the real host.
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "example.com"
    assert request.extensions.get("sni_hostname") == "example.com"
