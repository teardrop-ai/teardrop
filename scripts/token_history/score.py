# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Score watchlist signals against realised forward returns.

For every as-of date, signals are computed point-in-time (``signals.py``) and
paired with each token's forward price return over each horizon. Results are
reported against two baselines: an equal-weight basket of every scored token
that day (``vs EW``) and holding the benchmark (``vs BTC``).

    python -m scripts.token_history.score --sqlite path/to/token_history.db
    python -m scripts.token_history.score                    # $NEON_DATABASE_URL, read-only
    python -m scripts.token_history.score --horizons 7,30 --split-date 2026-09-01 --csv obs.csv

Caveats printed with every report: the history is survivor-biased wherever
pruning removed dropped tokens, and daily as-of dates with multi-day horizons
overlap, so ``t_adj`` scales the t-stat to roughly non-overlapping samples.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import os
import sqlite3
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scripts.token_history.signals import (
    BUCKET_ACTIVE,
    BUCKET_HEALTHY,
    BUCKET_PUMP,
    NUMERIC_SIGNALS,
    HistoryRow,
    Panel,
    Params,
    compute_signals,
)

DEFAULT_HORIZONS = (7, 30, 90)
DEFAULT_TOLERANCE_DAYS = 2
DEFAULT_TRAIN_FRACTION = 0.7
MIN_IC_PAIRS = 5

_SELECT_COLUMNS = (
    "token_id",
    "timestamp",
    "current_price",
    "change_7d",
    "change_30d",
    "change_1yr",
    "fdv",
    "market_cap",
    "volume_24h",
    "ath_change_pct",
)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def parse_timestamp(value: str | dt.datetime) -> dt.datetime:
    """Return an aware UTC datetime; naive values are treated as UTC."""
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _to_rows(records: Sequence[Sequence[object]]) -> list[HistoryRow]:
    return [HistoryRow(r[0], parse_timestamp(r[1]), *r[2:]) for r in records]  # type: ignore[arg-type]


def load_sqlite(path: str | Path) -> list[HistoryRow]:
    """Read token_history from a local SQLite copy (read-only; crypto rows only when asset_type exists)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(token_history)")}
        where = " WHERE asset_type = 'crypto'" if "asset_type" in columns else ""
        records = conn.execute(f"SELECT {', '.join(_SELECT_COLUMNS)} FROM token_history{where}").fetchall()
    finally:
        conn.close()
    return _to_rows(records)


def load_postgres(dsn: str, table: str = "token_history") -> list[HistoryRow]:
    """Read token_history from Postgres inside a READ ONLY transaction."""
    import psycopg
    from psycopg import sql

    query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(map(sql.Identifier, _SELECT_COLUMNS)), sql.Identifier(table))
    with psycopg.connect(dsn, autocommit=True, connect_timeout=60, prepare_threshold=None) as conn:
        conn.read_only = True
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(query)
            records = cur.fetchall()
    return _to_rows(records)


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    as_of: dt.date
    token_id: str
    bucket: str | None
    removal_candidate: bool
    values: dict[str, float | None]
    forward: dict[int, float | None] = field(default_factory=dict)
    vs_ew: dict[int, float | None] = field(default_factory=dict)
    vs_btc: dict[int, float | None] = field(default_factory=dict)


def forward_return(panel: Panel, row: HistoryRow, horizon: int, tolerance: int = DEFAULT_TOLERANCE_DAYS) -> float | None:
    """% price change from *row* to the first row within [day+h, day+h+tolerance]."""
    if not row.current_price or row.current_price <= 0:
        return None
    start = row.day + dt.timedelta(days=horizon)
    exit_row = panel.first_in_range(row.token_id, start, start + dt.timedelta(days=tolerance))
    if exit_row is None or exit_row.current_price is None:
        return None
    return (exit_row.current_price / row.current_price - 1.0) * 100.0


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_observations(
    panel: Panel,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    params: Params | None = None,
    *,
    step: int = 1,
    min_tokens: int = 20,
    tolerance: int = DEFAULT_TOLERANCE_DAYS,
) -> list[Observation]:
    """Point-in-time signals + forward returns for every ``step``-th eligible as-of date.

    A date is eligible when at least ``min_tokens`` tokens have a price that day.
    """
    params = params or Params()
    eligible = [
        d for d in panel.dates() if sum(1 for t in panel.tokens if (r := panel.get(t, d)) and r.current_price) >= min_tokens
    ]
    observations: list[Observation] = []
    for as_of in eligible[:: max(1, step)]:
        signals = compute_signals(panel, as_of, params)
        day_obs = [Observation(as_of, s.token_id, s.bucket, s.removal_candidate, s.values) for s in signals.values()]
        for h in horizons:
            fwd = {o.token_id: forward_return(panel, signals[o.token_id].row, h, tolerance) for o in day_obs}
            ew = _mean([v for v in fwd.values() if v is not None])
            btc = fwd.get(params.benchmark_token)
            for o in day_obs:
                f = fwd[o.token_id]
                o.forward[h] = f
                o.vs_ew[h] = None if f is None or ew is None else f - ew
                o.vs_btc[h] = None if f is None or btc is None else f - btc
        observations.extend(day_obs)
    return observations


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation; None when undefined (too few pairs or a constant series)."""
    if len(xs) < MIN_IC_PAIRS:
        return None
    try:
        return statistics.correlation(xs, ys, method="ranked")
    except statistics.StatisticsError:
        return None


def adjusted_t(values: Sequence[float], horizon: int, step: int) -> float | None:
    """t-stat of the mean using ~non-overlapping sample size n * step / horizon.

    None when fewer than 2 effectively independent windows exist: overlapping
    windows are near-copies, so their low dispersion would inflate the t-stat.
    """
    if len(values) < 2:
        return None
    eff_n = len(values) * min(1.0, step / horizon)
    if eff_n < 2:
        return None
    sd = statistics.stdev(values)
    return None if sd < 1e-9 else statistics.fmean(values) / (sd / math.sqrt(eff_n))


def split_date_for(observations: Sequence[Observation], horizon: int, fraction: float = DEFAULT_TRAIN_FRACTION) -> dt.date | None:
    """First as-of date of the test period: the date at *fraction* of dates with a forward return."""
    dates = sorted({o.as_of for o in observations if o.forward.get(horizon) is not None})
    if len(dates) < 2:
        return None
    return dates[min(len(dates) - 1, max(1, int(len(dates) * fraction)))]


@dataclass
class GroupStats:
    label: str
    n: int
    dates: int
    mean_forward: float | None
    vs_ew: float | None
    vs_btc: float | None
    hit_rate: float | None
    t_adj: float | None
    vs_ew_train: float | None
    vs_ew_test: float | None


GROUPS: tuple[tuple[str, Callable[[Observation], bool]], ...] = (
    ("all scored", lambda o: True),
    (BUCKET_HEALTHY, lambda o: o.bucket == BUCKET_HEALTHY),
    (BUCKET_PUMP, lambda o: o.bucket == BUCKET_PUMP),
    (BUCKET_ACTIVE, lambda o: o.bucket == BUCKET_ACTIVE),
    ("removal candidate", lambda o: o.removal_candidate),
)


def group_stats(observations: Sequence[Observation], horizon: int, split: dt.date | None, step: int = 1) -> list[GroupStats]:
    out = []
    for label, predicate in GROUPS:
        obs = [o for o in observations if predicate(o) and o.forward.get(horizon) is not None]
        ew = [o.vs_ew[horizon] for o in obs if o.vs_ew[horizon] is not None]
        btc = [o.vs_btc[horizon] for o in obs if o.vs_btc[horizon] is not None]
        train = [o.vs_ew[horizon] for o in obs if split and o.as_of < split and o.vs_ew[horizon] is not None]
        test = [o.vs_ew[horizon] for o in obs if split and o.as_of >= split and o.vs_ew[horizon] is not None]
        daily: dict[dt.date, list[float]] = {}
        for o in obs:
            if o.vs_ew[horizon] is not None:
                daily.setdefault(o.as_of, []).append(o.vs_ew[horizon])
        out.append(
            GroupStats(
                label=label,
                n=len(obs),
                dates=len({o.as_of for o in obs}),
                mean_forward=_mean([o.forward[horizon] for o in obs]),
                vs_ew=_mean(ew),
                vs_btc=_mean(btc),
                hit_rate=_mean([100.0 if v > 0 else 0.0 for v in ew]),
                t_adj=adjusted_t([statistics.fmean(v) for v in daily.values()], horizon, step),
                vs_ew_train=_mean(train),
                vs_ew_test=_mean(test),
            )
        )
    return out


@dataclass
class SignalStats:
    signal: str
    dates: int
    mean_ic: float | None
    t_adj: float | None
    ic_train: float | None
    ic_test: float | None
    q_spread: float | None


def _daily_ic_and_spread(obs: Sequence[Observation], signal: str, horizon: int) -> tuple[float | None, float | None]:
    pairs = sorted(
        (
            (o.values[signal], o.forward[horizon])
            for o in obs
            if o.values.get(signal) is not None and o.forward.get(horizon) is not None
        ),
        key=lambda p: p[0],  # signal only: tie order must not depend on the outcome
    )
    ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    q = len(pairs) // 5
    spread = None
    if q >= 1:
        spread = statistics.fmean(p[1] for p in pairs[-q:]) - statistics.fmean(p[1] for p in pairs[:q])
    return ic, spread


def signal_stats(observations: Sequence[Observation], horizon: int, split: dt.date | None, step: int) -> list[SignalStats]:
    by_date: dict[dt.date, list[Observation]] = {}
    for o in observations:
        by_date.setdefault(o.as_of, []).append(o)

    out = []
    for signal in NUMERIC_SIGNALS:
        ics: list[tuple[dt.date, float]] = []
        spreads: list[float] = []
        for day, obs in sorted(by_date.items()):
            ic, spread = _daily_ic_and_spread(obs, signal, horizon)
            if ic is not None:
                ics.append((day, ic))
            if spread is not None:
                spreads.append(spread)
        values = [v for _, v in ics]
        out.append(
            SignalStats(
                signal=signal,
                dates=len(values),
                mean_ic=_mean(values),
                t_adj=adjusted_t(values, horizon, step),
                ic_train=_mean([v for d, v in ics if split and d < split]),
                ic_test=_mean([v for d, v in ics if split and d >= split]),
                q_spread=_mean(spreads),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt(value: float | int | None, spec: str = "+.2f") -> str:
    if value is None:
        return "n/a"
    return format(value, spec) if isinstance(value, float) else str(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)), "  ".join("-" * w for w in widths)]
    lines += ["  ".join(c.rjust(w) if i else c.ljust(w) for i, (c, w) in enumerate(zip(r, widths))) for r in rows]
    return "\n".join(lines)


def render_report(
    observations: Sequence[Observation],
    horizons: Sequence[int],
    params: Params,
    *,
    source: str,
    step: int,
    split_override: dt.date | None = None,
) -> str:
    dates = sorted({o.as_of for o in observations})
    out = [
        f"Signal scoring report  source={source}  params={params.version}",
        f"as-of dates: {len(dates)} ({dates[0]} .. {dates[-1]})  step={step}d  tokens={len({o.token_id for o in observations})}"
        if dates
        else "as-of dates: 0",
        "Returns are % price change; 'vs EW' = minus the equal-weight mean of all scored tokens that day,",
        f"'vs BTC' = minus {params.benchmark_token}. Hit% = share beating EW. IC = daily Spearman(signal, forward return).",
        "CAVEATS: survivor bias where dropped tokens were pruned; overlapping windows (use t_adj, not raw n);",
        "         small samples - treat |t_adj| < 2 or train/test sign flips as noise.",
    ]
    for h in horizons:
        split = split_override or split_date_for(observations, h)
        out.append("")
        out.append(f"=== Horizon {h}d  (test period starts {split or 'n/a'}) ===")
        groups = group_stats(observations, h, split, step)
        out.append(
            _table(
                ["group", "n", "dates", "mean fwd", "vs EW", "vs BTC", "hit%", "t_adj", "vs EW train", "vs EW test"],
                [
                    [
                        g.label,
                        str(g.n),
                        str(g.dates),
                        _fmt(g.mean_forward),
                        _fmt(g.vs_ew),
                        _fmt(g.vs_btc),
                        _fmt(g.hit_rate, ".0f"),
                        _fmt(g.t_adj),
                        _fmt(g.vs_ew_train),
                        _fmt(g.vs_ew_test),
                    ]
                    for g in groups
                ],
            )
        )
        out.append("")
        sigs = signal_stats(observations, h, split, step)
        out.append(
            _table(
                ["signal", "dates", "mean IC", "t_adj", "IC train", "IC test", "Q5-Q1 fwd"],
                [
                    [
                        s.signal,
                        str(s.dates),
                        _fmt(s.mean_ic, "+.3f"),
                        _fmt(s.t_adj, "+.2f"),
                        _fmt(s.ic_train, "+.3f"),
                        _fmt(s.ic_test, "+.3f"),
                        _fmt(s.q_spread),
                    ]
                    for s in sigs
                ],
            )
        )
    return "\n".join(out)


def write_csv(path: str | Path, observations: Sequence[Observation], horizons: Sequence[int]) -> None:
    headers = ["as_of", "token_id", "bucket", "removal_candidate", *NUMERIC_SIGNALS]
    for h in horizons:
        headers += [f"fwd_{h}d", f"vs_ew_{h}d", f"vs_btc_{h}d"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for o in observations:
            row: list[object] = [o.as_of.isoformat(), o.token_id, o.bucket or "", int(o.removal_candidate)]
            row += [o.values.get(s) for s in NUMERIC_SIGNALS]
            for h in horizons:
                row += [o.forward.get(h), o.vs_ew.get(h), o.vs_btc.get(h)]
            writer.writerow(["" if v is None else v for v in row])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score watchlist signals against forward returns.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sqlite", help="local SQLite copy of token_history")
    source.add_argument("--dsn", help="Postgres DSN (default: $NEON_DATABASE_URL); read-only")
    parser.add_argument("--table", default="token_history")
    parser.add_argument("--horizons", default=",".join(map(str, DEFAULT_HORIZONS)), help="comma-separated days")
    parser.add_argument("--step", type=int, default=1, help="days between as-of dates (default 1)")
    parser.add_argument("--min-tokens", type=int, default=20, help="min priced tokens for an as-of date")
    parser.add_argument("--split-date", type=dt.date.fromisoformat, help="first test-period date (default: 70%% of dates)")
    parser.add_argument("--csv", help="also write one row per (as_of, token) observation")
    args = parser.parse_args(argv)
    try:
        args.horizons = sorted({int(h) for h in args.horizons.split(",") if h.strip()})
    except ValueError:
        parser.error("--horizons must be comma-separated integers")
    if not args.horizons or min(args.horizons) <= 0 or args.step <= 0:
        parser.error("--horizons and --step must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.sqlite:
        rows, source = load_sqlite(args.sqlite), f"sqlite:{Path(args.sqlite).name}"
    else:
        dsn = args.dsn or os.getenv("NEON_DATABASE_URL", "")
        if not dsn:
            print("[error] pass --sqlite PATH or set NEON_DATABASE_URL / --dsn", file=sys.stderr)
            return 2
        rows, source = load_postgres(dsn, args.table), f"postgres:{args.table}"

    params = Params()
    observations = build_observations(Panel(rows), args.horizons, params, step=args.step, min_tokens=args.min_tokens)
    if not observations:
        print("[error] no eligible as-of dates (need priced rows; see --min-tokens)", file=sys.stderr)
        return 1
    print(render_report(observations, args.horizons, params, source=source, step=args.step, split_override=args.split_date))
    if args.csv:
        write_csv(args.csv, observations, args.horizons)
        print(f"\nWrote {len(observations)} observations to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
