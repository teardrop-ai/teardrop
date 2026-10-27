# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Daily CoinGecko snapshot of the private watchlist into Neon ``token_history``.

Render cron job (Python runtime), run from the repo root:
    Build:    pip install -r scripts/token_history/requirements.txt
    Command:  python -m scripts.token_history.fetch_tokens
    Schedule: 0 0 * * *

Local checks:
    python -m scripts.token_history.fetch_tokens --check-watchlist --watchlist watchlist.txt
    python -m scripts.token_history.fetch_tokens --dry-run      # read-only DB, no writes

Environment variables:
    NEON_DATABASE_URL        Postgres DSN for the existing token_history table (required)
    WATCHLIST_PATH           Watchlist file, e.g. /etc/secrets/watchlist.txt (required to write)
    COINGECKO_API_KEY        Demo or Pro key (Pro is detected from COINGECKO_BASE_URL)
    COINGECKO_BASE_URL       Default https://api.coingecko.com/api/v3
    TOKEN_HISTORY_PRUNE      "false" disables pruning (same as --no-prune); default true
    TOKEN_HISTORY_MAX_PRUNE  Max tokens one run may prune before aborting; default 5

Rows are written with ``ON CONFLICT (token_id, timestamp) DO NOTHING`` and tokens
that already have a row for the current UTC day are skipped, so re-running a
failed or partial job only fills the gaps. Tokens removed from the watchlist have
their history deleted only when pruning is enabled, every watchlist id resolved on
CoinGecko this run, and no more than ``TOKEN_HISTORY_MAX_PRUNE`` tokens would go.
The job never runs DDL; the table schema is verified before any write.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import os
import random
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from scripts.token_history.watchlist import Watchlist, WatchlistError, load_watchlist

DEFAULT_BASE_URL = "https://api.coingecko.com/api/v3"
DEFAULT_TABLE = "token_history"
DEFAULT_MAX_PRUNE = 5

COLUMNS = (
    "token_id",
    "name",
    "symbol",
    "timestamp",
    "change_1yr",
    "fdv",
    "market_cap",
    "volume_24h",
    "current_price",
    "change_7d",
    "change_30d",
    "ath",
    "ath_change_pct",
)

# /coins/markets accepts 250 ids, but Cloudflare 403s query strings past ~1640 chars.
_BATCH_SIZE = 250
_MAX_IDS_QUERY_CHARS = 1500
_BACKOFF_RETRIES = 7
_FALSEY = {"0", "false", "no", "off"}

Row = tuple[Any, ...]


def _log(message: str = "") -> None:
    print(message, flush=True)


def _err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# CoinGecko
# ---------------------------------------------------------------------------


class CoinGeckoError(RuntimeError):
    """Raised when a CoinGecko request fails after all retries."""


class RateLimiter:
    """Sliding-window limiter; 28 req/min keeps a margin under the Demo 30 req/min cap."""

    def __init__(
        self,
        max_rpm: int = 28,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_rpm = max_rpm
        self.window_seconds = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._times: collections.deque[float] = collections.deque()

    def wait(self) -> None:
        now = self._clock()
        while self._times and self._times[0] <= now - self.window_seconds:
            self._times.popleft()
        if len(self._times) >= self.max_rpm:
            delay = self._times[0] + self.window_seconds - now
            if delay > 0:
                self._sleep(delay)
                now = self._clock()
            self._times.popleft()
        self._times.append(now)

    def backoff(self, seconds: float) -> None:
        self._sleep(seconds)
        self._times.clear()


def chunk_by_query_length(
    token_ids: Sequence[str], max_chars: int = _MAX_IDS_QUERY_CHARS, max_count: int = _BATCH_SIZE
) -> list[list[str]]:
    """Greedily batch ids so each comma-joined batch respects both caps."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for token_id in token_ids:
        extra = len(token_id) + (1 if current else 0)
        if current and (current_len + extra > max_chars or len(current) >= max_count):
            batches.append(current)
            current, current_len, extra = [], 0, len(token_id)
        current.append(token_id)
        current_len += extra
    if current:
        batches.append(current)
    return batches


class CoinGeckoClient:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = _BACKOFF_RETRIES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30.0)
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self._sleep = sleep
        self.retries = retries
        self.headers: dict[str, str] = {}
        if api_key:
            header = "x-cg-pro-api-key" if "pro-api." in self.base_url else "x-cg-demo-api-key"
            self.headers[header] = api_key

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        last_error = "no attempts made"
        for attempt in range(self.retries):
            self.limiter.wait()
            try:
                resp = self.http.get(url, params=params, headers=self.headers)
            except httpx.TransportError as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
                self._sleep(random.uniform(0, min(60, 2**attempt)))
                continue
            if resp.status_code == 429:
                try:
                    wait = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    wait = float(2**attempt)
                last_error = "HTTP 429"
                _log(f"  [rate limit] 429, waiting {wait:.0f}s (attempt {attempt + 1}/{self.retries})")
                self.limiter.backoff(min(wait, 120.0))
                continue
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                self._sleep(random.uniform(0, min(60, 2**attempt)))
                continue
            if resp.status_code >= 400:
                raise CoinGeckoError(f"GET {path} returned HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise CoinGeckoError(f"GET {path} failed after {self.retries} attempts ({last_error})")

    def fetch_markets(self, token_ids: Sequence[str]) -> list[dict[str, Any]]:
        params = {
            "vs_currency": "usd",
            "ids": ",".join(token_ids),
            "price_change_percentage": "7d,30d,1y",
            "per_page": _BATCH_SIZE,
            "page": 1,
        }
        data = self._get("/coins/markets", params)
        if not isinstance(data, list):
            raise CoinGeckoError(f"/coins/markets returned {type(data).__name__}, expected list")
        return data

    def fetch_change_1yr(self, token_id: str) -> float | None:
        """1-year % change from daily prices; covers coins younger than a year too."""
        data = self._get(f"/coins/{token_id}/market_chart", {"vs_currency": "usd", "days": 365, "interval": "daily"})
        prices = data.get("prices", []) if isinstance(data, dict) else []
        if len(prices) < 2 or not prices[0][1]:
            return None
        return (prices[-1][1] - prices[0][1]) / prices[0][1] * 100


def build_row(coin: dict[str, Any], timestamp: dt.datetime, change_1yr: float | None) -> Row:
    values = {
        "token_id": coin["id"],
        "name": coin.get("name"),
        "symbol": coin.get("symbol"),
        "timestamp": timestamp,
        "change_1yr": change_1yr,
        "fdv": coin.get("fully_diluted_valuation"),
        "market_cap": coin.get("market_cap"),
        "volume_24h": coin.get("total_volume"),
        "current_price": coin.get("current_price"),
        "change_7d": coin.get("price_change_percentage_7d_in_currency"),
        "change_30d": coin.get("price_change_percentage_30d_in_currency"),
        "ath": coin.get("ath"),
        "ath_change_pct": coin.get("ath_change_percentage"),
    }
    return tuple(values[col] for col in COLUMNS)


def _fmt(value: float | None, template: str) -> str:
    return "N/A" if value is None else template.format(value)


def collect_batch(client: CoinGeckoClient, batch: Sequence[str], timestamp: dt.datetime) -> tuple[list[Row], list[str]]:
    """Fetch one markets batch; return ``(rows, unresolved_ids)``.

    Ids missing from the response (unknown/delisted, or a failed request) are
    reported as unresolved and never written as all-NULL rows.
    """
    try:
        market = {coin["id"]: coin for coin in client.fetch_markets(batch) if isinstance(coin, dict) and "id" in coin}
    except (CoinGeckoError, httpx.HTTPError, ValueError) as exc:
        _err(f"[error] markets request for {len(batch)} token(s) failed: {exc}")
        return [], list(batch)

    rows: list[Row] = []
    unresolved: list[str] = []
    for token_id in batch:
        coin = market.get(token_id)
        if coin is None:
            unresolved.append(token_id)
            _err(f"  {token_id}: no market data (unknown id, delisted, or partial response)")
            continue
        change_1yr = coin.get("price_change_percentage_1y_in_currency")
        if change_1yr is None:
            try:
                change_1yr = client.fetch_change_1yr(token_id)
            except (CoinGeckoError, httpx.HTTPError, ValueError) as exc:
                _err(f"  {token_id}: market_chart fallback failed: {exc}")
        rows.append(build_row(coin, timestamp, change_1yr))
        _log(
            f"  {token_id}: change_1yr={_fmt(change_1yr, '{:.2f}%')}  "
            f"FDV={_fmt(coin.get('fully_diluted_valuation'), '${:,.0f}')}  "
            f"Volume 24h={_fmt(coin.get('total_volume'), '${:,.0f}')}"
        )
    return rows, unresolved


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class Store(Protocol):
    def columns(self) -> list[str]: ...
    def tokens_between(self, start: dt.datetime, end: dt.datetime) -> set[str]: ...
    def existing_token_ids(self) -> set[str]: ...
    def insert(self, rows: Sequence[Row]) -> int: ...
    def count_rows(self, token_ids: Sequence[str]) -> int: ...
    def delete(self, token_ids: Sequence[str]) -> int: ...


class PostgresStore:
    """Thin psycopg wrapper over an autocommit connection.

    Every statement runs in its own explicit transaction, so each batch commits on
    its own (partial runs persist) and ``conn.read_only`` applies to all of them.
    """

    def __init__(self, conn: Any, table: str = DEFAULT_TABLE) -> None:
        from psycopg import sql

        self.conn = conn
        self._sql = sql
        self._table = sql.Identifier(table)
        self._table_name = table

    def _fetch(self, query: Any, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def columns(self) -> list[str]:
        rows = self._fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
            (self._table_name,),
        )
        return [r[0] for r in rows]

    def tokens_between(self, start: dt.datetime, end: dt.datetime) -> set[str]:
        q = self._sql.SQL('SELECT DISTINCT token_id FROM {} WHERE "timestamp" >= %s AND "timestamp" < %s').format(self._table)
        return {r[0] for r in self._fetch(q, (start, end))}

    def existing_token_ids(self) -> set[str]:
        return {r[0] for r in self._fetch(self._sql.SQL("SELECT DISTINCT token_id FROM {}").format(self._table))}

    def insert(self, rows: Sequence[Row]) -> int:
        if not rows:
            return 0
        q = self._sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (token_id, "timestamp") DO NOTHING').format(
            self._table,
            self._sql.SQL(", ").join(map(self._sql.Identifier, COLUMNS)),
            self._sql.SQL(", ").join(self._sql.Placeholder() * len(COLUMNS)),
        )
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(q, rows)
            return max(cur.rowcount, 0)

    def count_rows(self, token_ids: Sequence[str]) -> int:
        q = self._sql.SQL("SELECT count(*) FROM {} WHERE token_id = ANY(%s)").format(self._table)
        return int(self._fetch(q, (list(token_ids),))[0][0])

    def delete(self, token_ids: Sequence[str]) -> int:
        q = self._sql.SQL("DELETE FROM {} WHERE token_id = ANY(%s)").format(self._table)
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(q, (list(token_ids),))
            return cur.rowcount


def schema_problems(actual: Iterable[str]) -> list[str]:
    actual = list(actual)
    if not actual:
        return ["table does not exist (this job never creates it)"]
    problems = []
    missing = [c for c in COLUMNS if c not in actual]
    extra = [c for c in actual if c not in COLUMNS]
    if missing:
        problems.append(f"missing columns: {', '.join(missing)}")
    if extra:
        problems.append(f"unexpected columns: {', '.join(extra)}")
    return problems


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


@dataclass
class PrunePlan:
    to_remove: list[str]
    allowed: bool
    reason: str
    blocking: bool = False


def plan_prune(
    existing: Iterable[str],
    watchlist: Watchlist,
    *,
    enabled: bool,
    unresolved: Sequence[str],
    max_prune: int,
) -> PrunePlan:
    """Decide whether history for tokens dropped from the watchlist may be deleted.

    ``blocking`` marks refusals that need a human (the run exits non-zero), as
    opposed to deliberate skips such as ``--no-prune``.
    """
    to_remove = sorted(set(existing) - set(watchlist.token_ids))
    if not to_remove:
        return PrunePlan([], False, "nothing to prune")
    if not enabled:
        return PrunePlan(to_remove, False, "pruning disabled (--no-prune / TOKEN_HISTORY_PRUNE=false)")
    if watchlist.is_sample:
        return PrunePlan(to_remove, False, "using the committed sample watchlist", blocking=True)
    if unresolved:
        return PrunePlan(
            to_remove,
            False,
            f"{len(unresolved)} watchlist id(s) did not resolve on CoinGecko this run "
            "(possible typo); fix the watchlist or re-run once CoinGecko recovers",
            blocking=True,
        )
    if len(to_remove) > max_prune:
        return PrunePlan(
            to_remove,
            False,
            f"{len(to_remove)} tokens would be pruned, above the limit of {max_prune}; if intended, "
            f"re-run once with --max-prune {len(to_remove)} (or raise TOKEN_HISTORY_MAX_PRUNE)",
            blocking=True,
        )
    return PrunePlan(to_remove, True, "ok")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    inserted: int = 0
    skipped_today: int = 0
    unresolved: list[str] = field(default_factory=list)
    pruned_rows: int = 0
    prune: PrunePlan | None = None
    exit_code: int = 0


def run(
    watchlist: Watchlist,
    store: Store,
    client: CoinGeckoClient,
    *,
    now: dt.datetime | None = None,
    dry_run: bool = False,
    prune: bool = True,
    max_prune: int = DEFAULT_MAX_PRUNE,
) -> RunResult:
    result = RunResult()

    problems = schema_problems(store.columns())
    if problems:
        _err("[error] token_history schema does not match the expected 13 columns:")
        for problem in problems:
            _err(f"  - {problem}")
        result.exit_code = 1
        return result

    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0)
    done_today = store.tokens_between(day_start, day_start + dt.timedelta(days=1))
    pending = [t for t in watchlist.token_ids if t not in done_today]
    result.skipped_today = len(watchlist.token_ids) - len(pending)

    _log(f"Watchlist: {len(watchlist.token_ids)} token(s) from {watchlist.source}")
    if watchlist.duplicates:
        _log(f"Ignored duplicate id(s): {', '.join(sorted(set(watchlist.duplicates)))}")
    if result.skipped_today:
        _log(f"Skipping {result.skipped_today} token(s) already stored for {day_start:%Y-%m-%d} UTC.")
    if pending:
        _log(f"Fetching {len(pending)} token(s) at {now.isoformat()} ...")

    for batch in chunk_by_query_length(pending):
        rows, unresolved = collect_batch(client, batch, now)
        result.unresolved.extend(unresolved)
        if dry_run:
            result.inserted += len(rows)
        else:
            result.inserted += store.insert(rows)

    plan = plan_prune(
        store.existing_token_ids(),
        watchlist,
        enabled=prune,
        unresolved=result.unresolved,
        max_prune=max_prune,
    )
    result.prune = plan
    if plan.to_remove:
        _log(f"Not in watchlist: {', '.join(plan.to_remove)}")
    if plan.allowed:
        if dry_run:
            result.pruned_rows = store.count_rows(plan.to_remove)
            _log(f"[dry run] would delete {result.pruned_rows} row(s) for {len(plan.to_remove)} token(s).")
        else:
            result.pruned_rows = store.delete(plan.to_remove)
            _log(f"Pruned {result.pruned_rows} row(s) for {len(plan.to_remove)} token(s).")
    elif plan.to_remove:
        (_err if plan.blocking else _log)(f"{'[error] ' if plan.blocking else ''}Prune skipped: {plan.reason}")

    verb = "would insert" if dry_run else "inserted"
    _log(f"Done: {verb} {result.inserted} row(s), {len(result.unresolved)} unresolved, {result.pruned_rows} pruned.")
    if result.unresolved:
        _err(f"[error] unresolved id(s): {', '.join(result.unresolved)}")
    if result.unresolved or plan.blocking:
        result.exit_code = 1
    return result


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else raw.strip().lower() not in _FALSEY


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append a daily CoinGecko snapshot of the watchlist to Neon token_history.")
    parser.add_argument("--watchlist", default=None, help="watchlist file (default: $WATCHLIST_PATH)")
    parser.add_argument("--dsn", default=None, help="Postgres DSN (default: $NEON_DATABASE_URL)")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="target table (default: token_history)")
    parser.add_argument("--dry-run", action="store_true", help="read-only DB session; report without writing")
    parser.add_argument("--check-watchlist", action="store_true", help="validate the watchlist and exit (no network)")
    parser.add_argument(
        "--no-prune",
        dest="prune",
        action="store_false",
        default=_env_bool("TOKEN_HISTORY_PRUNE", True),
        help="never delete history for tokens removed from the watchlist",
    )
    parser.add_argument(
        "--max-prune",
        type=int,
        default=int(os.getenv("TOKEN_HISTORY_MAX_PRUNE", "") or DEFAULT_MAX_PRUNE),
        help=f"abort pruning above this many tokens (default: $TOKEN_HISTORY_MAX_PRUNE or {DEFAULT_MAX_PRUNE})",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        watchlist = load_watchlist(args.watchlist)
    except WatchlistError as exc:
        _err(f"[error] {exc}")
        return 2

    if args.check_watchlist:
        _log(f"OK: {len(watchlist.token_ids)} token id(s) in {watchlist.source}")
        if watchlist.duplicates:
            _log(f"Duplicates ignored: {', '.join(sorted(set(watchlist.duplicates)))}")
        return 0

    if watchlist.is_sample and not args.dry_run:
        _err("[error] WATCHLIST_PATH is not set; refusing to write using the committed sample watchlist.")
        return 2

    dsn = args.dsn or os.getenv("NEON_DATABASE_URL", "")
    if not dsn:
        _err("[error] NEON_DATABASE_URL is not set (or pass --dsn).")
        return 2

    import psycopg

    client = CoinGeckoClient(
        api_key=os.getenv("COINGECKO_API_KEY", ""),
        base_url=os.getenv("COINGECKO_BASE_URL", "") or DEFAULT_BASE_URL,
    )
    # prepare_threshold=None keeps the job compatible with Neon's PgBouncer pooler.
    with psycopg.connect(dsn, autocommit=True, connect_timeout=60, prepare_threshold=None) as conn:
        conn.read_only = args.dry_run
        if args.dry_run:
            _log("[dry run] read-only session; nothing will be written.")
        try:
            result = run(
                watchlist,
                PostgresStore(conn, args.table),
                client,
                dry_run=args.dry_run,
                prune=args.prune,
                max_prune=args.max_prune,
            )
        finally:
            client.http.close()
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
