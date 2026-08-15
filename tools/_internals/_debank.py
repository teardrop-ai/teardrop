# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Small DeBank Cloud adapter for wallet-level analytics."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

from teardrop.config import get_settings
from tools._internals._http_session import get_debank_session
from tools._internals.provenance import utc_now_iso

logger = logging.getLogger(__name__)

_DEBANK_BASE_URL = "https://pro-openapi.debank.com/v1"
_CACHE_TTL_SECONDS = 60
_CACHE_MAX_ENTRIES = 256
_REQUEST_TIMEOUT_SECONDS = 15


@dataclass
class DebankError:
    operation: str
    error_type: str
    message: str


@dataclass
class DebankWalletSnapshot:
    total_balance: dict[str, Any] | None
    protocol_positions: list[dict[str, Any]]
    token_balances: list[dict[str, Any]]
    errors: list[DebankError]
    source_fetched_at: str | None
    source_urls: list[str]
    cache_hit: bool = False


@dataclass
class DebankEndpointSnapshot:
    payload: Any | None
    error: DebankError | None
    source_fetched_at: str | None
    source_url: str
    cache_hit: bool = False


_wallet_cache: dict[tuple[str, bool, bool], tuple[float, DebankWalletSnapshot]] = {}
_endpoint_cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, DebankEndpointSnapshot]] = {}
_ENDPOINT_CACHE_MAX_ENTRIES = 512


def _endpoint_url(path: str, params: Mapping[str, str | int] | None = None) -> str:
    url = f"{_DEBANK_BASE_URL}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


def _source_urls(include_net_worth: bool, include_token_balances: bool) -> list[str]:
    urls = [_endpoint_url("user/all_complex_protocol_list")]
    if include_net_worth:
        urls.append(_endpoint_url("user/total_balance"))
    if include_token_balances:
        urls.append(_endpoint_url("user/all_token_list"))
    return urls


def _api_key() -> str:
    return get_settings().debank_api_key.strip()


async def _get_json(
    path: str,
    operation: str,
    *,
    params: Mapping[str, str | int] | None = None,
) -> tuple[Any | None, DebankError | None]:
    """Fetch one DeBank endpoint without retrying billable provider requests."""
    api_key = _api_key()
    if not api_key:
        return None, DebankError(operation, "configuration", "DEBANK_API_KEY is not configured")

    try:
        session = await get_debank_session()
        async with session.get(
            _endpoint_url(path, params),
            headers={"AccessKey": api_key, "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
        ) as response:
            if response.status != 200:
                error_type = "authentication_error" if response.status in {401, 403} else "upstream_error"
                if response.status == 429:
                    error_type = "rate_limited"
                return None, DebankError(
                    operation,
                    error_type,
                    f"DeBank {operation} returned HTTP {response.status}",
                )
            try:
                return await response.json(content_type=None), None
            except (TypeError, ValueError) as exc:
                logger.warning("DeBank %s returned malformed JSON: %s", operation, exc)
                return None, DebankError(operation, "malformed_response", "DeBank returned malformed JSON")
    except asyncio.TimeoutError:
        return None, DebankError(operation, "timeout", f"DeBank {operation} timed out")
    except (aiohttp.ClientError, OSError) as exc:
        logger.warning("DeBank %s request failed: %s", operation, type(exc).__name__)
        return None, DebankError(operation, "upstream_error", f"DeBank {operation} request failed")


def _params_key(params: Mapping[str, str | int] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in (params or {}).items()))


async def _get_cached_json(
    path: str,
    operation: str,
    *,
    params: Mapping[str, str | int] | None = None,
) -> DebankEndpointSnapshot:
    """Fetch a billable endpoint with a short-lived cache and safe provenance."""
    cache_key = (path, _params_key(params))
    now = time.monotonic()
    cached = _endpoint_cache.get(cache_key)
    if cached is not None and now < cached[0]:
        snapshot = copy.deepcopy(cached[1])
        snapshot.cache_hit = True
        return snapshot

    payload, error = await _get_json(path, operation, params=params)
    snapshot = DebankEndpointSnapshot(
        payload=payload,
        error=error,
        source_fetched_at=utc_now_iso() if error is None else None,
        source_url=_endpoint_url(path),
    )
    if error is None:
        if len(_endpoint_cache) >= _ENDPOINT_CACHE_MAX_ENTRIES:
            oldest_key = min(_endpoint_cache, key=lambda key: _endpoint_cache[key][0])
            _endpoint_cache.pop(oldest_key, None)
        _endpoint_cache[cache_key] = (now + _CACHE_TTL_SECONDS, copy.deepcopy(snapshot))
    return snapshot


async def get_wallet_approvals(wallet_address: str, chain_id: str) -> DebankEndpointSnapshot:
    """Return the cached DeBank token-authorized list for one chain."""
    return await _get_cached_json(
        "user/token_authorized_list",
        "token_authorized_list",
        params={"id": wallet_address, "chain_id": chain_id},
    )


async def get_wallet_history(
    wallet_address: str,
    *,
    chain_ids: list[str] | None = None,
    start_time: int | None = None,
    page_count: int = 20,
) -> DebankEndpointSnapshot:
    """Return one cached page of DeBank's all-chain wallet history."""
    params: dict[str, str | int] = {"id": wallet_address, "page_count": page_count}
    if chain_ids:
        params["chain_ids"] = ",".join(chain_ids)
    if start_time is not None:
        params["start_time"] = start_time
    return await _get_cached_json("user/all_history_list", "all_history_list", params=params)


async def get_wallet_positions(
    wallet_address: str,
    *,
    include_net_worth: bool = True,
    include_token_balances: bool = False,
) -> DebankWalletSnapshot:
    """Return cached or fresh all-chain protocol positions for an EVM wallet."""
    cache_key = (wallet_address.lower(), include_net_worth, include_token_balances)
    now = time.monotonic()
    cached = _wallet_cache.get(cache_key)
    if cached is not None and now < cached[0]:
        snapshot = copy.deepcopy(cached[1])
        snapshot.cache_hit = True
        return snapshot

    errors: list[DebankError] = []
    protocol_positions: list[dict[str, Any]] = []
    total_balance: dict[str, Any] | None = None

    if not _api_key():
        return DebankWalletSnapshot(
            total_balance=None,
            protocol_positions=[],
            token_balances=[],
            errors=[DebankError("provider", "configuration", "DEBANK_API_KEY is not configured")],
            source_fetched_at=None,
            source_urls=_source_urls(include_net_worth, include_token_balances),
        )

    protocol_payload, protocol_error = await _get_json(
        "user/all_complex_protocol_list",
        "all_complex_protocol_list",
    )
    if protocol_error is not None:
        errors.append(protocol_error)
    elif not isinstance(protocol_payload, list):
        errors.append(DebankError("all_complex_protocol_list", "malformed_response", "Expected a JSON list"))
    else:
        protocol_positions = [item for item in protocol_payload if isinstance(item, dict)]

    if include_net_worth:
        balance_payload, balance_error = await _get_json("user/total_balance", "total_balance")
        if balance_error is not None:
            errors.append(balance_error)
        elif not isinstance(balance_payload, dict):
            errors.append(DebankError("total_balance", "malformed_response", "Expected a JSON object"))
        else:
            total_balance = balance_payload

    token_balances: list[dict[str, Any]] = []
    if include_token_balances:
        token_payload, token_error = await _get_json(
            "user/all_token_list",
            "all_token_list",
            params={"id": wallet_address},
        )
        if token_error is not None:
            errors.append(token_error)
        elif not isinstance(token_payload, list):
            errors.append(DebankError("all_token_list", "malformed_response", "Expected a JSON list"))
        else:
            token_balances = [item for item in token_payload if isinstance(item, dict)]

    source_fetched_at = utc_now_iso() if not errors else None
    snapshot = DebankWalletSnapshot(
        total_balance=total_balance,
        protocol_positions=protocol_positions,
        token_balances=token_balances,
        errors=errors,
        source_fetched_at=source_fetched_at,
        source_urls=_source_urls(include_net_worth, include_token_balances),
    )
    if not errors:
        if len(_wallet_cache) >= _CACHE_MAX_ENTRIES:
            oldest_key = min(_wallet_cache, key=lambda key: _wallet_cache[key][0])
            _wallet_cache.pop(oldest_key, None)
        _wallet_cache[cache_key] = (now + _CACHE_TTL_SECONDS, copy.deepcopy(snapshot))
    return snapshot


def clear_wallet_cache() -> None:
    """Clear cached DeBank wallet responses; intended for tests and maintenance."""
    _wallet_cache.clear()
    _endpoint_cache.clear()
