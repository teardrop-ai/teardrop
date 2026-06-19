"""Helpers for deriving a client IP through trusted reverse proxies."""

from __future__ import annotations

import ipaddress


def _normalized_ip(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def client_ip_from_request(request, *, trusted_proxy_count: int) -> str:
    """Return the caller IP, honoring a bounded number of trusted proxy hops.

    When ``trusted_proxy_count`` is zero, the direct peer is always used. For
    positive counts, the function treats ``X-Forwarded-For`` as a chain of
    addresses ending at the direct peer and returns the client immediately
    before the trusted proxy suffix. Malformed forwarded values fail closed to
    the direct peer address.
    """

    peer_host = ""
    if getattr(request, "client", None) is not None:
        peer_host = str(getattr(request.client, "host", "") or "").strip()

    if trusted_proxy_count <= 0:
        return peer_host

    forwarded_for = str(request.headers.get("x-forwarded-for", "") or "")
    forwarded_chain = [normalized for segment in forwarded_for.split(",") if (normalized := _normalized_ip(segment)) is not None]
    peer_ip = _normalized_ip(peer_host)
    if not forwarded_chain or peer_ip is None:
        return peer_host

    full_chain = [*forwarded_chain, peer_ip]
    candidate_index = len(full_chain) - trusted_proxy_count - 1
    if candidate_index < 0:
        return peer_host
    return full_chain[candidate_index]
