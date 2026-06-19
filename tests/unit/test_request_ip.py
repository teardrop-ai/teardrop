from __future__ import annotations

from types import SimpleNamespace

from shared.request_ip import client_ip_from_request


def _request(*, forwarded_for: str = "", peer_host: str = "203.0.113.10"):
    headers = {}
    if forwarded_for:
        headers["x-forwarded-for"] = forwarded_for
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer_host))


def test_ignores_xff_when_trusted_proxy_count_is_zero():
    request = _request(forwarded_for="198.51.100.7")

    assert client_ip_from_request(request, trusted_proxy_count=0) == "203.0.113.10"


def test_returns_client_behind_one_trusted_proxy():
    request = _request(forwarded_for="198.51.100.7", peer_host="10.0.0.2")

    assert client_ip_from_request(request, trusted_proxy_count=1) == "198.51.100.7"


def test_ignores_left_prepended_spoofed_hops():
    request = _request(forwarded_for="203.0.113.66, 198.51.100.7", peer_host="10.0.0.2")

    assert client_ip_from_request(request, trusted_proxy_count=1) == "198.51.100.7"


def test_falls_back_to_peer_for_malformed_forwarded_chain():
    request = _request(forwarded_for="unknown, not-an-ip", peer_host="10.0.0.2")

    assert client_ip_from_request(request, trusted_proxy_count=1) == "10.0.0.2"


def test_supports_ipv6_clients():
    request = _request(forwarded_for="2001:db8::5", peer_host="2001:db8::10")

    assert client_ip_from_request(request, trusted_proxy_count=1) == "2001:db8::5"
