# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Point-in-time watchlist signals computed from token_history.

Ports the metric definitions of TokenHistory's ``verify_volume.py`` health
report (turnover, float ratio, momentum acceleration, trend/drift, turnover
buckets, removal candidates) into pure functions over a daily panel.

``compute_signals(panel, as_of)`` reads only rows dated on or before ``as_of``,
so a signal for a past date is exactly what the report would have shown that
day. Scoring depends on this: adding later rows must never change it.
"""

from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

NUMERIC_SIGNALS = (
    "change_7d",
    "change_30d",
    "change_1yr",
    "accel",
    "turnover_pct",
    "turnover_trend",
    "float_ratio_pct",
    "float_drift",
    "ath_change_pct",
    "vs_btc_1yr",
)

BUCKET_HEALTHY = "healthy"
BUCKET_PUMP = "pump risk"
BUCKET_ACTIVE = "down but active"


@dataclass(frozen=True)
class HistoryRow:
    token_id: str
    timestamp: dt.datetime
    current_price: float | None = None
    change_7d: float | None = None
    change_30d: float | None = None
    change_1yr: float | None = None
    fdv: float | None = None
    market_cap: float | None = None
    volume_24h: float | None = None
    ath_change_pct: float | None = None

    @property
    def day(self) -> dt.date:
        return self.timestamp.astimezone(dt.timezone.utc).date()


@dataclass(frozen=True)
class Params:
    """Thresholds; defaults match verify_volume.py so results are comparable."""

    healthy_min_return_pct: float = 50
    healthy_min_turnover_pct: float = 5
    pump_max_turnover_pct: float = 1
    active_min_turnover_pct: float = 5
    removal_max_turnover_pct: float = 1
    removal_min_span_days: int = 30
    removal_recent_window_days: int = 30
    delta_window_days: int = 30
    max_staleness_days: int = 1
    benchmark_token: str = "bitcoin"

    @property
    def version(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()[:8]


@dataclass
class TokenSignals:
    token_id: str
    as_of: dt.date
    row: HistoryRow
    values: dict[str, float | None] = field(default_factory=dict)
    bucket: str | None = None
    removal_candidate: bool = False


# ---------------------------------------------------------------------------
# Metric helpers (same semantics as verify_volume.py)
# ---------------------------------------------------------------------------


def turnover_pct(volume_24h: float | None, fdv: float | None) -> float | None:
    """24h volume / FDV * 100; None when either is missing or fdv <= 0."""
    if volume_24h is None or fdv is None or fdv <= 0:
        return None
    return 100.0 * volume_24h / fdv


def float_ratio_pct(market_cap: float | None, fdv: float | None) -> float | None:
    """Market cap / FDV * 100; None when undefined or implausible (mcap <= 0 or > fdv)."""
    if market_cap is None or fdv is None or fdv <= 0 or market_cap <= 0 or market_cap > fdv:
        return None
    return 100.0 * market_cap / fdv


def acceleration(change_7d: float | None, change_30d: float | None) -> float | None:
    """7d change minus the weekly pace implied by the 30d change; > 0 means turning up."""
    if change_7d is None or change_30d is None:
        return None
    return change_7d - change_30d / 4.0


def _diff(new: float | None, old: float | None) -> float | None:
    return None if new is None or old is None else new - old


# ---------------------------------------------------------------------------
# Daily panel
# ---------------------------------------------------------------------------


class Panel:
    """One row per token per UTC day (the latest that day), with sorted dates per token."""

    def __init__(self, rows: Iterable[HistoryRow]) -> None:
        by_token: dict[str, dict[dt.date, HistoryRow]] = {}
        for row in rows:
            days = by_token.setdefault(row.token_id, {})
            existing = days.get(row.day)
            if existing is None or row.timestamp > existing.timestamp:
                days[row.day] = row
        self._rows = by_token
        self._dates = {token: sorted(days) for token, days in by_token.items()}

    @property
    def tokens(self) -> list[str]:
        return sorted(self._rows)

    def dates(self) -> list[dt.date]:
        return sorted({d for dates in self._dates.values() for d in dates})

    def get(self, token_id: str, day: dt.date) -> HistoryRow | None:
        return self._rows.get(token_id, {}).get(day)

    def latest_on_or_before(self, token_id: str, day: dt.date) -> HistoryRow | None:
        dates = self._dates.get(token_id, [])
        idx = bisect.bisect_right(dates, day)
        return self._rows[token_id][dates[idx - 1]] if idx else None

    def latest_at_or_before(self, token_id: str, moment: dt.datetime) -> HistoryRow | None:
        """Latest row whose timestamp is <= *moment* (exact-time, not calendar-day, cutoff)."""
        row = self.latest_on_or_before(token_id, moment.astimezone(dt.timezone.utc).date())
        if row is not None and row.timestamp > moment:
            row = self.latest_on_or_before(token_id, row.day - dt.timedelta(days=1))
        return row

    def first_in_range(self, token_id: str, start: dt.date, end: dt.date) -> HistoryRow | None:
        """Earliest row dated within [start, end]."""
        dates = self._dates.get(token_id, [])
        idx = bisect.bisect_left(dates, start)
        if idx < len(dates) and dates[idx] <= end:
            return self._rows[token_id][dates[idx]]
        return None

    def rows_up_to(self, token_id: str, day: dt.date) -> list[HistoryRow]:
        dates = self._dates.get(token_id, [])
        return [self._rows[token_id][d] for d in dates[: bisect.bisect_right(dates, day)]]


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def classify_bucket(change_1yr: float | None, turnover: float | None, params: Params) -> str | None:
    if change_1yr is None or turnover is None:
        return None
    if change_1yr > params.healthy_min_return_pct and turnover > params.healthy_min_turnover_pct:
        return BUCKET_HEALTHY
    if change_1yr > params.healthy_min_return_pct and turnover < params.pump_max_turnover_pct:
        return BUCKET_PUMP
    if change_1yr < 0 and turnover > params.active_min_turnover_pct:
        return BUCKET_ACTIVE
    return None


def is_removal_candidate(history: list[HistoryRow], params: Params) -> bool:
    """Thin turnover sustained over >= min span AND still thin in the recent window.

    *history* must be the token's rows up to the evaluation date, oldest first.
    """
    if not history:
        return False
    low_days = [
        r.day for r in history if (t := turnover_pct(r.volume_24h, r.fdv)) is not None and t < params.removal_max_turnover_pct
    ]
    if not low_days or (max(low_days) - min(low_days)).days < params.removal_min_span_days:
        return False
    cutoff = history[-1].day - dt.timedelta(days=params.removal_recent_window_days)
    recent = [t for r in history if r.day > cutoff and (t := turnover_pct(r.volume_24h, r.fdv)) is not None]
    return bool(recent) and sum(recent) / len(recent) < params.removal_max_turnover_pct


def compute_signals(panel: Panel, as_of: dt.date, params: Params | None = None) -> dict[str, TokenSignals]:
    """Signals for every token with a row no older than ``max_staleness_days`` before *as_of*."""
    params = params or Params()
    oldest_allowed = as_of - dt.timedelta(days=params.max_staleness_days)
    prior_min_age = dt.timedelta(days=max(1, params.delta_window_days - 5))

    bench = panel.latest_on_or_before(params.benchmark_token, as_of)
    bench_1yr = bench.change_1yr if bench is not None and bench.day >= oldest_allowed else None

    result: dict[str, TokenSignals] = {}
    for token_id in panel.tokens:
        row = panel.latest_on_or_before(token_id, as_of)
        if row is None or row.day < oldest_allowed:
            continue
        prior = panel.latest_at_or_before(token_id, row.timestamp - prior_min_age)

        turnover = turnover_pct(row.volume_24h, row.fdv)
        float_now = float_ratio_pct(row.market_cap, row.fdv)
        values: dict[str, float | None] = {
            "change_7d": row.change_7d,
            "change_30d": row.change_30d,
            "change_1yr": row.change_1yr,
            "accel": acceleration(row.change_7d, row.change_30d),
            "turnover_pct": turnover,
            "turnover_trend": _diff(turnover, turnover_pct(prior.volume_24h, prior.fdv) if prior else None),
            "float_ratio_pct": float_now,
            "float_drift": _diff(float_now, float_ratio_pct(prior.market_cap, prior.fdv) if prior else None),
            "ath_change_pct": row.ath_change_pct,
            "vs_btc_1yr": _diff(row.change_1yr, bench_1yr),
        }
        result[token_id] = TokenSignals(
            token_id=token_id,
            as_of=as_of,
            row=row,
            values=values,
            bucket=classify_bucket(row.change_1yr, turnover, params),
            removal_candidate=is_removal_candidate(panel.rows_up_to(token_id, row.day), params),
        )
    return result
