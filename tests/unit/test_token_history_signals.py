# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for scripts/token_history/signals.py — point-in-time watchlist signals."""

from __future__ import annotations

import datetime as dt

import pytest

from scripts.token_history import signals as sig

D0 = dt.date(2026, 6, 1)


def _row(token_id: str, day: dt.date, hour: int = 0, **values) -> sig.HistoryRow:
    ts = dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.timezone.utc)
    return sig.HistoryRow(token_id=token_id, timestamp=ts, **values)


def _day(n: int) -> dt.date:
    return D0 + dt.timedelta(days=n)


class TestMetricHelpers:
    @pytest.mark.parametrize(
        ("volume", "fdv", "expected"),
        [(5.0, 100.0, 5.0), (None, 100.0, None), (5.0, None, None), (5.0, 0.0, None), (5.0, -1.0, None)],
    )
    def test_turnover(self, volume, fdv, expected):
        assert sig.turnover_pct(volume, fdv) == expected

    @pytest.mark.parametrize(
        ("mcap", "fdv", "expected"),
        [(50.0, 100.0, 50.0), (100.0, 100.0, 100.0), (0.0, 100.0, None), (101.0, 100.0, None), (50.0, 0.0, None)],
    )
    def test_float_ratio_rejects_implausible_values(self, mcap, fdv, expected):
        assert sig.float_ratio_pct(mcap, fdv) == expected

    def test_acceleration(self):
        assert sig.acceleration(10.0, 20.0) == 5.0
        assert sig.acceleration(None, 20.0) is None


class TestClassifyBucket:
    p = sig.Params()

    @pytest.mark.parametrize(
        ("c1y", "turnover", "bucket"),
        [
            (60, 6, sig.BUCKET_HEALTHY),
            (60, 0.5, sig.BUCKET_PUMP),
            (-10, 6, sig.BUCKET_ACTIVE),
            (50, 6, None),  # boundaries are strict, as in verify_volume.py
            (60, 3, None),
            (-10, 5, None),
            (None, 6, None),
        ],
    )
    def test_buckets(self, c1y, turnover, bucket):
        assert sig.classify_bucket(c1y, turnover, self.p) == bucket


class TestPanel:
    def test_keeps_latest_row_per_utc_day(self):
        panel = sig.Panel([_row("a", D0, 1, current_price=1.0), _row("a", D0, 5, current_price=2.0)])
        assert panel.get("a", D0).current_price == 2.0

    def test_lookups(self):
        panel = sig.Panel([_row("a", _day(0)), _row("a", _day(5))])
        assert panel.latest_on_or_before("a", _day(4)).day == _day(0)
        assert panel.latest_on_or_before("a", _day(-1)) is None
        assert panel.first_in_range("a", _day(1), _day(5)).day == _day(5)
        assert panel.first_in_range("a", _day(1), _day(4)) is None
        assert [r.day for r in panel.rows_up_to("a", _day(4))] == [_day(0)]

    def test_latest_at_or_before_uses_exact_time(self):
        panel = sig.Panel([_row("a", _day(0), 0), _row("a", _day(1), 12)])
        noon = dt.datetime(_day(1).year, _day(1).month, _day(1).day, 12, tzinfo=dt.timezone.utc)
        assert panel.latest_at_or_before("a", noon).day == _day(1)
        assert panel.latest_at_or_before("a", noon - dt.timedelta(seconds=1)).day == _day(0)


class TestRemovalCandidate:
    p = sig.Params()

    def _history(self, turnovers: list[float]) -> list[sig.HistoryRow]:
        return [_row("x", _day(i), volume_24h=t, fdv=100.0) for i, t in enumerate(turnovers)]

    def test_sustained_and_still_thin(self):
        assert sig.is_removal_candidate(self._history([0.5] * 40), self.p) is True

    def test_too_short_span(self):
        assert sig.is_removal_candidate(self._history([0.5] * 20), self.p) is False

    def test_recovered_liquidity_is_excluded(self):
        assert sig.is_removal_candidate(self._history([0.5] * 40 + [3.0] * 30), self.p) is False


class TestComputeSignals:
    def _rows(self):
        return [
            _row("bitcoin", _day(0), change_1yr=10.0, fdv=100.0, volume_24h=1.0, market_cap=90.0),
            _row("bitcoin", _day(30), change_1yr=20.0, fdv=100.0, volume_24h=2.0, market_cap=95.0),
            _row(
                "alt",
                _day(0),
                change_1yr=70.0,
                fdv=100.0,
                volume_24h=2.0,
                market_cap=40.0,
            ),
            _row(
                "alt",
                _day(30),
                change_1yr=80.0,
                change_7d=10.0,
                change_30d=20.0,
                fdv=100.0,
                volume_24h=6.0,
                market_cap=50.0,
                ath_change_pct=-30.0,
            ),
            _row("stale", _day(25), change_1yr=1.0, fdv=100.0, volume_24h=1.0),
        ]

    def _panel(self):
        return sig.Panel(self._rows())

    def test_values_trends_and_bucket(self):
        result = sig.compute_signals(self._panel(), _day(30))
        alt = result["alt"]
        assert alt.values["turnover_pct"] == 6.0
        assert alt.values["turnover_trend"] == 4.0
        assert alt.values["float_ratio_pct"] == 50.0
        assert alt.values["float_drift"] == 10.0
        assert alt.values["accel"] == 5.0
        assert alt.values["vs_btc_1yr"] == 60.0
        assert alt.values["ath_change_pct"] == -30.0
        assert alt.bucket == sig.BUCKET_HEALTHY
        assert set(alt.values) == set(sig.NUMERIC_SIGNALS)

    def test_stale_tokens_are_excluded(self):
        assert "stale" not in sig.compute_signals(self._panel(), _day(30))

    def test_no_prior_row_means_no_trend(self):
        alt = sig.compute_signals(self._panel(), _day(0))["alt"]
        assert alt.values["turnover_trend"] is None
        assert alt.values["float_drift"] is None

    def test_no_lookahead(self):
        """Adding rows after as_of must not change any signal for as_of."""
        future_rows = [_row("bitcoin", _day(31), change_1yr=99.0), _row("alt", _day(31), change_1yr=-99.0, fdv=1.0)]
        before = sig.compute_signals(self._panel(), _day(30))
        after = sig.compute_signals(sig.Panel(self._rows() + future_rows), _day(30))
        assert {k: (v.values, v.bucket, v.removal_candidate) for k, v in before.items()} == {
            k: (v.values, v.bucket, v.removal_candidate) for k, v in after.items()
        }

    def test_params_version_tracks_thresholds(self):
        assert sig.Params().version == sig.Params().version
        assert sig.Params().version != sig.Params(healthy_min_return_pct=40).version
