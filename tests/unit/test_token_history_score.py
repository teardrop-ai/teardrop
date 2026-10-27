# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for scripts/token_history/score.py — signal scoring against forward returns."""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3

import pytest

from scripts.token_history import score
from scripts.token_history.signals import HistoryRow, Panel, Params

D0 = dt.date(2026, 6, 1)


def _ts(day: dt.date, hour: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.timezone.utc)


def _day(n: int) -> dt.date:
    return D0 + dt.timedelta(days=n)


def _synthetic_rows(tokens: int = 10, days: int = 40) -> list[HistoryRow]:
    """Token i grows (1 + 0.001*i)^d per day and reports change_7d = i, so change_7d ranks forward returns perfectly."""
    names = ["bitcoin"] + [f"tok{i:02d}" for i in range(1, tokens)]
    return [
        HistoryRow(
            token_id=name,
            timestamp=_ts(_day(d)),
            current_price=100.0 * (1 + 0.001 * i) ** d,
            change_7d=float(i),
            fdv=1000.0,
            volume_24h=10.0,
            market_cap=500.0,
        )
        for i, name in enumerate(names)
        for d in range(days)
    ]


class TestForwardReturn:
    def test_exact_tolerance_and_missing(self):
        panel = Panel(
            [
                HistoryRow("a", _ts(_day(0)), current_price=100.0),
                HistoryRow("a", _ts(_day(8)), current_price=110.0),
                HistoryRow("a", _ts(_day(9)), current_price=120.0),
            ]
        )
        start = panel.get("a", _day(0))
        assert score.forward_return(panel, start, 7, tolerance=2) == pytest.approx(10.0)  # first row in [7, 9]
        assert score.forward_return(panel, start, 7, tolerance=0) is None
        assert score.forward_return(panel, start, 30) is None

    def test_unpriced_entry(self):
        panel = Panel([HistoryRow("a", _ts(_day(0)), current_price=0.0), HistoryRow("a", _ts(_day(7)), current_price=1.0)])
        assert score.forward_return(panel, panel.get("a", _day(0)), 7) is None


class TestStatistics:
    def test_spearman(self):
        assert score.spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)
        assert score.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
        assert score.spearman([1, 2, 3], [1, 2, 3]) is None  # too few pairs
        assert score.spearman([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None  # constant

    def test_adjusted_t_penalises_overlap(self):
        values = [0.1, 0.2, 0.15, 0.05, 0.12, 0.18, 0.09, 0.11]
        assert score.adjusted_t(values, horizon=3, step=1) < score.adjusted_t(values, horizon=1, step=1)
        assert score.adjusted_t(values, horizon=30, step=1) is None  # < 2 independent windows
        assert score.adjusted_t([0.1], 7, 1) is None
        assert score.adjusted_t([0.1, 0.1], 1, 1) is None  # zero variance

    def test_quintile_spread_ignores_outcome_when_signal_ties(self):
        obs = [score.Observation(D0, f"t{i}", None, False, {"change_7d": 1.0}, forward={7: float(i)}) for i in range(10)]
        _, spread = score._daily_ic_and_spread(obs, "change_7d", 7)
        # Ties keep input order: top quintile = t8,t9 (8.5), bottom = t0,t1 (0.5). Outcome-sorting would also give
        # 8.0 here, so reverse the input to prove the order is not re-sorted by outcome.
        assert spread == pytest.approx(8.0)
        _, spread_rev = score._daily_ic_and_spread(list(reversed(obs)), "change_7d", 7)
        assert spread_rev == pytest.approx(-8.0)


class TestBuildObservations:
    def test_predictive_signal_scores_ic_one(self):
        obs = score.build_observations(Panel(_synthetic_rows()), horizons=(7,), min_tokens=5)
        stats = {s.signal: s for s in score.signal_stats(obs, 7, split=None, step=1)}
        assert stats["change_7d"].mean_ic == pytest.approx(1.0)
        assert stats["change_7d"].q_spread > 0
        assert stats["change_30d"].dates == 0 and stats["change_30d"].mean_ic is None

    def test_baselines(self):
        obs = score.build_observations(Panel(_synthetic_rows()), horizons=(7,), min_tokens=5)
        day0 = [o for o in obs if o.as_of == _day(0)]
        assert sum(o.vs_ew[7] for o in day0) == pytest.approx(0.0, abs=1e-9)
        assert next(o for o in day0 if o.token_id == "bitcoin").vs_btc[7] == pytest.approx(0.0)
        # Last 7 days have no exit price yet.
        assert all(o.forward[7] is None for o in obs if o.as_of > _day(32))

    def test_min_tokens_and_step(self):
        panel = Panel(_synthetic_rows(tokens=5, days=10))
        assert score.build_observations(panel, horizons=(7,), min_tokens=6) == []
        dates = {o.as_of for o in score.build_observations(panel, horizons=(7,), min_tokens=5, step=3)}
        assert dates == {_day(0), _day(3), _day(6), _day(9)}

    def test_split_partitions_train_and_test(self):
        obs = score.build_observations(Panel(_synthetic_rows()), horizons=(7,), min_tokens=5)
        split = score.split_date_for(obs, 7)
        assert _day(0) < split <= _day(32)
        groups = {g.label: g for g in score.group_stats(obs, 7, split)}
        assert groups["all scored"].vs_ew_train == pytest.approx(0.0, abs=1e-9)
        assert groups["all scored"].vs_ew_test == pytest.approx(0.0, abs=1e-9)


def _write_sqlite(path, with_asset_type: bool = True) -> None:
    conn = sqlite3.connect(path)
    cols = ", ".join(f"{c} {'TEXT' if c in ('token_id', 'timestamp') else 'REAL'}" for c in score._SELECT_COLUMNS)
    conn.execute(f"CREATE TABLE token_history ({cols}{', asset_type TEXT' if with_asset_type else ''})")
    for row in _synthetic_rows(tokens=6, days=20):
        values = [getattr(row, c) for c in score._SELECT_COLUMNS]
        values[1] = row.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            f"INSERT INTO token_history VALUES ({', '.join('?' * (len(values) + with_asset_type))})",
            values + (["crypto"] if with_asset_type else []),
        )
    if with_asset_type:
        conn.execute(
            "INSERT INTO token_history (token_id, timestamp, current_price, asset_type) VALUES ('AAPL', '2026-06-01', 1, 'stock')"
        )
    conn.commit()
    conn.close()


class TestLoaders:
    def test_parse_timestamp_normalises_to_aware_utc(self):
        assert score.parse_timestamp("2026-06-01T00:00:00Z") == _ts(D0)
        assert score.parse_timestamp("2026-06-01 00:00:00") == _ts(D0)
        assert score.parse_timestamp("2026-06-01T02:00:00+02:00") == _ts(D0)

    def test_load_sqlite_filters_non_crypto_and_is_read_only(self, tmp_path):
        db = tmp_path / "th copy.db"
        _write_sqlite(db)
        rows = score.load_sqlite(db)
        assert {r.token_id for r in rows} == {"bitcoin", "tok01", "tok02", "tok03", "tok04", "tok05"}
        assert all(r.timestamp.tzinfo is not None for r in rows)

    def test_load_sqlite_without_asset_type_column(self, tmp_path):
        db = tmp_path / "plain.db"
        _write_sqlite(db, with_asset_type=False)
        assert len(score.load_sqlite(db)) == 6 * 20


class TestMain:
    def test_end_to_end_report_and_csv(self, tmp_path, capsys):
        db, out = tmp_path / "th.db", tmp_path / "obs.csv"
        _write_sqlite(db)
        assert score.main(["--sqlite", str(db), "--horizons", "7", "--min-tokens", "5", "--csv", str(out)]) == 0
        report = capsys.readouterr().out
        assert f"params={Params().version}" in report
        assert "=== Horizon 7d" in report and "change_7d" in report
        with open(out, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 6 * 20
        assert {"fwd_7d", "vs_ew_7d", "vs_btc_7d"} <= set(rows[0])

    def test_requires_a_source(self, monkeypatch):
        monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
        assert score.main([]) == 2

    def test_no_eligible_dates(self, tmp_path):
        db = tmp_path / "th.db"
        _write_sqlite(db)
        assert score.main(["--sqlite", str(db), "--min-tokens", "99"]) == 1

    @pytest.mark.parametrize("bad", [["--horizons", "x"], ["--horizons", "0"], ["--step", "0"]])
    def test_rejects_bad_args(self, bad):
        with pytest.raises(SystemExit):
            score._parse_args(bad)
