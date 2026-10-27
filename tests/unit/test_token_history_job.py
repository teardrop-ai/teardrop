# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for scripts/token_history — the daily Neon token_history cron job."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from scripts.token_history import fetch_tokens as job
from scripts.token_history.watchlist import SAMPLE_PATH, Watchlist, WatchlistError, load_watchlist, parse_watchlist

NOW = dt.datetime(2026, 10, 27, 0, 0, 5, 123456, tzinfo=dt.timezone.utc)


def _coin(token_id: str, **overrides):
    coin = {
        "id": token_id,
        "name": token_id.title(),
        "symbol": token_id[:3],
        "fully_diluted_valuation": 1000.0,
        "market_cap": 900.0,
        "total_volume": 50.0,
        "current_price": 1.5,
        "price_change_percentage_7d_in_currency": 2.0,
        "price_change_percentage_30d_in_currency": 3.0,
        "price_change_percentage_1y_in_currency": 40.0,
        "ath": 9.0,
        "ath_change_percentage": -80.0,
    }
    coin.update(overrides)
    return coin


class FakeClient:
    def __init__(self, coins=(), chart=None, fail_markets=False):
        self.coins = {c["id"]: c for c in coins}
        self.chart = chart or {}
        self.fail_markets = fail_markets
        self.market_calls: list[list[str]] = []

    def fetch_markets(self, token_ids):
        self.market_calls.append(list(token_ids))
        if self.fail_markets:
            raise job.CoinGeckoError("boom")
        return [self.coins[t] for t in token_ids if t in self.coins]

    def fetch_change_1yr(self, token_id):
        return self.chart.get(token_id)


class FakeStore:
    def __init__(self, existing=(), today=(), columns=job.COLUMNS):
        self._columns = list(columns)
        self.existing = set(existing)
        self.today = set(today)
        self.inserted: list[tuple] = []
        self.deleted: list[str] = []

    def columns(self):
        return self._columns

    def tokens_between(self, start, end):
        assert start == dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)
        assert end - start == dt.timedelta(days=1)
        return set(self.today)

    def existing_token_ids(self):
        return set(self.existing) | {row[0] for row in self.inserted}

    def insert(self, rows):
        self.inserted.extend(rows)
        return len(rows)

    def count_rows(self, token_ids):
        return 10 * len(token_ids)

    def delete(self, token_ids):
        self.deleted.extend(token_ids)
        return 10 * len(token_ids)


def _wl(*ids, is_sample=False):
    return Watchlist(token_ids=list(ids), source="test", is_sample=is_sample)


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


class TestParseWatchlist:
    def test_accepts_comments_blank_lines_and_pasted_python_list_lines(self):
        text = '# header\n\nbitcoin\n  Ethereum   # inline\n"solana",\n# "dynex", -- closed out\nwrapped.token_x\n'
        ids, dupes = parse_watchlist(text)
        assert ids == ["bitcoin", "ethereum", "solana", "wrapped.token_x"]
        assert dupes == []

    def test_dedupes_preserving_first_occurrence(self):
        ids, dupes = parse_watchlist("bitcoin\nethereum\nbitcoin\n")
        assert ids == ["bitcoin", "ethereum"]
        assert dupes == ["bitcoin"]

    def test_invalid_line_fails_closed_with_line_number(self):
        with pytest.raises(WatchlistError, match=r"wl:2: invalid CoinGecko id 'WATCHLIST = \['"):
            parse_watchlist("bitcoin\nWATCHLIST = [\n", source="wl")

    def test_empty_watchlist_is_an_error(self):
        with pytest.raises(WatchlistError, match="no token ids"):
            parse_watchlist("# only comments\n\n")


class TestLoadWatchlist:
    def test_configured_missing_path_never_falls_back_to_sample(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WATCHLIST_PATH", str(tmp_path / "missing.txt"))
        with pytest.raises(WatchlistError, match="does not exist"):
            load_watchlist()

    def test_unset_path_uses_sample_and_flags_it(self, monkeypatch):
        monkeypatch.delenv("WATCHLIST_PATH", raising=False)
        wl = load_watchlist()
        assert wl.is_sample is True
        assert wl.source == str(SAMPLE_PATH)
        assert wl.token_ids

    def test_env_path_is_loaded_as_production_list(self, monkeypatch, tmp_path):
        path = tmp_path / "watchlist.txt"
        path.write_text("\ufeffbitcoin\nethereum\n", encoding="utf-8")
        monkeypatch.setenv("WATCHLIST_PATH", str(path))
        wl = load_watchlist()
        assert wl.token_ids == ["bitcoin", "ethereum"]
        assert wl.is_sample is False


# ---------------------------------------------------------------------------
# CoinGecko client
# ---------------------------------------------------------------------------


def test_chunking_respects_length_and_count_caps():
    ids = [f"token-{i:03d}" for i in range(30)]  # 9 chars each
    batches = job.chunk_by_query_length(ids, max_chars=50, max_count=4)
    assert [t for b in batches for t in b] == ids
    assert all(len(b) <= 4 and len(",".join(b)) <= 50 for b in batches)


def _client(handler, **kwargs):
    sleeps: list[float] = []
    client = job.CoinGeckoClient(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        limiter=job.RateLimiter(max_rpm=1000, sleep=sleeps.append),
        **kwargs,
    )
    return client, sleeps


class TestCoinGeckoClient:
    def test_retries_429_and_5xx_then_succeeds(self):
        responses = iter([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(502), httpx.Response(200, json=[])])
        client, sleeps = _client(lambda request: next(responses))
        assert client.fetch_markets(["bitcoin"]) == []
        assert sleeps[0] == 3.0
        assert len(sleeps) == 2

    def test_client_error_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(400, text="bad id")

        client, _ = _client(handler)
        with pytest.raises(job.CoinGeckoError, match="HTTP 400"):
            client.fetch_markets(["bitcoin"])
        assert len(calls) == 1

    def test_exhausted_retries_raise(self):
        client, _ = _client(lambda request: httpx.Response(503), retries=2)
        with pytest.raises(job.CoinGeckoError, match="after 2 attempts"):
            client.fetch_markets(["bitcoin"])

    @pytest.mark.parametrize(
        ("base_url", "header"),
        [(job.DEFAULT_BASE_URL, "x-cg-demo-api-key"), ("https://pro-api.coingecko.com/api/v3", "x-cg-pro-api-key")],
    )
    def test_api_key_header_matches_plan(self, base_url, header):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            seen["ids"] = request.url.params["ids"]
            return httpx.Response(200, json=[])

        client, _ = _client(handler, api_key="k", base_url=base_url)
        client.fetch_markets(["bitcoin", "ethereum"])
        assert seen[header] == "k"
        assert seen["ids"] == "bitcoin,ethereum"

    def test_change_1yr_from_market_chart(self):
        client, _ = _client(lambda request: httpx.Response(200, json={"prices": [[0, 2.0], [1, 3.0]]}))
        assert client.fetch_change_1yr("bitcoin") == pytest.approx(50.0)


def test_rate_limiter_sleeps_when_window_is_full():
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = job.RateLimiter(max_rpm=2, window_seconds=60, clock=lambda: clock[0], sleep=sleep)
    limiter.wait()
    clock[0] = 10.0
    limiter.wait()
    limiter.wait()
    assert sleeps == [50.0]


# ---------------------------------------------------------------------------
# run(): idempotency, writes, and prune guards
# ---------------------------------------------------------------------------


class TestRun:
    def test_inserts_pending_tokens_and_skips_ones_stored_today(self):
        store = FakeStore(existing={"bitcoin"}, today={"bitcoin"})
        client = FakeClient([_coin("bitcoin"), _coin("ethereum")])
        result = job.run(_wl("bitcoin", "ethereum"), store, client, now=NOW)

        assert result.exit_code == 0
        assert result.skipped_today == 1
        assert client.market_calls == [["ethereum"]]
        assert len(store.inserted) == 1
        row = dict(zip(job.COLUMNS, store.inserted[0]))
        assert row["token_id"] == "ethereum"
        assert row["timestamp"] == NOW.replace(microsecond=0)
        assert row["fdv"] == 1000.0 and row["volume_24h"] == 50.0 and row["ath_change_pct"] == -80.0

    def test_missing_1y_change_uses_market_chart_fallback(self):
        store = FakeStore()
        client = FakeClient([_coin("newcoin", price_change_percentage_1y_in_currency=None)], chart={"newcoin": 12.5})
        job.run(_wl("newcoin"), store, client, now=NOW)
        assert dict(zip(job.COLUMNS, store.inserted[0]))["change_1yr"] == 12.5

    def test_schema_mismatch_aborts_before_fetching(self):
        store = FakeStore(columns=[*job.COLUMNS, "asset_type"])
        client = FakeClient([_coin("bitcoin")])
        result = job.run(_wl("bitcoin"), store, client, now=NOW)
        assert result.exit_code == 1
        assert client.market_calls == []
        assert store.inserted == []

    def test_prunes_removed_tokens_within_limit(self):
        store = FakeStore(existing={"bitcoin", "dynex"})
        result = job.run(_wl("bitcoin"), store, FakeClient([_coin("bitcoin")]), now=NOW, max_prune=5)
        assert result.exit_code == 0
        assert store.deleted == ["dynex"]
        assert result.pruned_rows == 10

    def test_no_prune_keeps_history(self):
        store = FakeStore(existing={"bitcoin", "dynex"})
        result = job.run(_wl("bitcoin"), store, FakeClient([_coin("bitcoin")]), now=NOW, prune=False)
        assert result.exit_code == 0
        assert store.deleted == []
        assert result.prune.to_remove == ["dynex"]

    def test_prune_above_limit_is_blocked_and_fails_the_run(self):
        store = FakeStore(existing={"bitcoin", "a", "b", "c"})
        result = job.run(_wl("bitcoin"), store, FakeClient([_coin("bitcoin")]), now=NOW, max_prune=2)
        assert result.exit_code == 1
        assert store.deleted == []
        assert len(store.inserted) == 1  # the snapshot is still written

    def test_unresolved_id_blocks_prune_even_within_limit(self):
        # Typo: "bitcoin" edited to "bitcon" must not delete bitcoin's history.
        store = FakeStore(existing={"bitcoin", "ethereum"})
        result = job.run(_wl("bitcon", "ethereum"), store, FakeClient([_coin("ethereum")]), now=NOW, max_prune=5)
        assert result.exit_code == 1
        assert result.unresolved == ["bitcon"]
        assert store.deleted == []
        assert [r[0] for r in store.inserted] == ["ethereum"]

    def test_markets_failure_writes_nothing_and_fails(self):
        store = FakeStore(existing={"bitcoin", "old"})
        result = job.run(_wl("bitcoin"), store, FakeClient(fail_markets=True), now=NOW)
        assert result.exit_code == 1
        assert store.inserted == [] and store.deleted == []

    def test_dry_run_never_writes(self):
        store = FakeStore(existing={"bitcoin", "dynex"})
        result = job.run(_wl("bitcoin"), store, FakeClient([_coin("bitcoin")]), now=NOW, dry_run=True)
        assert result.exit_code == 0
        assert store.inserted == [] and store.deleted == []
        assert result.inserted == 1 and result.pruned_rows == 10

    def test_sample_watchlist_never_prunes(self):
        store = FakeStore(existing={"bitcoin", "dynex"})
        result = job.run(_wl("bitcoin", is_sample=True), store, FakeClient([_coin("bitcoin")]), now=NOW, dry_run=True)
        assert result.prune.allowed is False
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# main(): fail-closed configuration
# ---------------------------------------------------------------------------


class TestMain:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for name in ("WATCHLIST_PATH", "NEON_DATABASE_URL", "TOKEN_HISTORY_PRUNE", "TOKEN_HISTORY_MAX_PRUNE"):
            monkeypatch.delenv(name, raising=False)

    def test_refuses_to_write_with_sample_watchlist(self):
        assert job.main(["--dsn", "postgresql://unused"]) == 2

    def test_missing_configured_watchlist_exits_before_db(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WATCHLIST_PATH", str(tmp_path / "nope.txt"))
        assert job.main(["--dsn", "postgresql://unused"]) == 2

    def test_requires_dsn(self, tmp_path):
        path = tmp_path / "wl.txt"
        path.write_text("bitcoin\n", encoding="utf-8")
        assert job.main(["--watchlist", str(path)]) == 2

    def test_check_watchlist_needs_no_network(self, tmp_path, capsys):
        path = tmp_path / "wl.txt"
        path.write_text("bitcoin\nbitcoin\nethereum\n", encoding="utf-8")
        assert job.main(["--check-watchlist", "--watchlist", str(path)]) == 0
        assert "OK: 2 token id(s)" in capsys.readouterr().out

    def test_prune_env_flags(self, monkeypatch):
        monkeypatch.setenv("TOKEN_HISTORY_PRUNE", "false")
        monkeypatch.setenv("TOKEN_HISTORY_MAX_PRUNE", "9")
        args = job._parse_args([])
        assert args.prune is False and args.max_prune == 9
        monkeypatch.setenv("TOKEN_HISTORY_PRUNE", "true")
        assert job._parse_args([]).prune is True
        assert job._parse_args(["--no-prune"]).prune is False
