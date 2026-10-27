# token_history cron job

Appends a daily CoinGecko snapshot of a **private** token watchlist to the
existing Neon `token_history` table (13 columns, PK `(token_id, timestamp)`).
The watchlist lives in a Render Secret File, never in this public repo.

## Render setup (dashboard, one time)

1. **New → Cron Job**, connect this repo, runtime **Python 3**.
   - Build command: `pip install -r scripts/token_history/requirements.txt`
   - Command: `python -m scripts.token_history.fetch_tokens`
   - Schedule: `0 0 * * *` (UTC)
   - Optional: set Auto-Deploy to **Off** so app pushes don't rebuild the job.
2. **Environment → Environment Variables**
   | Key | Value |
   |---|---|
   | `NEON_DATABASE_URL` | pooled Neon DSN for the DB holding `token_history` |
   | `WATCHLIST_PATH` | `/etc/secrets/watchlist.txt` |
   | `COINGECKO_API_KEY` | Demo key (Pro: also set `COINGECKO_BASE_URL=https://pro-api.coingecko.com/api/v3`) |
   | `PYTHON_VERSION` | `3.12.14` (matches the app image) |
   | `TOKEN_HISTORY_MAX_PRUNE` | optional, default `5` |
   | `TOKEN_HISTORY_PRUNE` | `false` recommended: keeps removed tokens' history so scoring isn't survivor-biased |
3. **Environment → Secret Files → Add**: filename `watchlist.txt`, one CoinGecko id per line.
4. **Runs → Trigger Run** once and check the log.

## Editing the watchlist

Edit the `watchlist.txt` secret file and save. The next run picks it up.

```text
# comments and blank lines are fine
bitcoin
ethereum      # inline comment
"solana",     # lines pasted from the old watchlist.py also work
```

Validate an edit locally before pasting it into the dashboard (no network or DB):

```bash
python -m scripts.token_history.fetch_tokens --check-watchlist --watchlist watchlist.txt
```

- **Add a token:** add its id; it starts collecting on the next run.
- **Remove a token:** delete the line. That token's **entire history is deleted**
  on the next run, subject to the guards below. Use `TOKEN_HISTORY_PRUNE=false`
  (or `--no-prune`) to keep history for removed tokens.

## Safety behaviour

| Situation | Result |
|---|---|
| `WATCHLIST_PATH` set but file missing, empty, or has an invalid line | exit 2, nothing fetched or written |
| `WATCHLIST_PATH` unset | exit 2 (the committed sample is only for `--dry-run`/`--check-watchlist`) |
| Table columns differ from the 13 expected | exit 1, nothing written (the job never runs DDL) |
| An id gets no CoinGecko data (typo, delisted, API failure) | other rows written; **prune skipped**; exit 1 |
| More than `TOKEN_HISTORY_MAX_PRUNE` tokens would be pruned | snapshot written; **prune skipped**; exit 1. Re-run once with `--max-prune N` if intended |
| Token already has a row for today (UTC) | skipped; reruns only fill gaps |

A failed run (non-zero exit) shows as failed in Render, so enable failure
notifications on the cron job. Fix the cause, then use **Trigger Run** to retry
safely.

## Local dry run

`--dry-run` uses a read-only Postgres session: it fetches from CoinGecko and
reports what would be inserted and pruned without writing:

```bash
NEON_DATABASE_URL=... WATCHLIST_PATH=watchlist.txt python -m scripts.token_history.fetch_tokens --dry-run
```

## Signal scoring

`signals.py` computes the health report's metrics (turnover, float ratio, accel,
turnover trend, float drift, ATH drawdown, vs BTC, turnover buckets, removal
candidates) for any past date using only rows up to that date. `score.py` pairs
those signals with each token's forward price return and reports, per horizon:

- **Groups** (buckets, removal candidates): mean forward return, excess vs an
  equal-weight basket of all scored tokens (`vs EW`) and vs BTC, hit rate,
  overlap-adjusted `t_adj`, and train/test split of `vs EW`.
- **Signals**: mean daily Spearman IC, `t_adj`, train/test IC, and top-minus-bottom
  quintile forward return.

```bash
python -m scripts.token_history.score --sqlite path/to/token_history.db   # local copy
python -m scripts.token_history.score                                     # $NEON_DATABASE_URL, read-only
python -m scripts.token_history.score --horizons 7,30 --split-date 2026-09-01 --csv obs.csv
```

Read results skeptically: forward returns need `current_price` (collected since
2026-05-31), long horizons have few dates, and history pruned before
`TOKEN_HISTORY_PRUNE=false` makes results survivor-biased. Trust a signal only
when `|t_adj| >= 2` and train and test agree in sign.
