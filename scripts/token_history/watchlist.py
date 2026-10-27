# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Load the private CoinGecko token watchlist for the token_history job.

The production watchlist is a Render Secret File (``WATCHLIST_PATH``, e.g.
``/etc/secrets/watchlist.txt``) so it never lands in this public repo. When
``WATCHLIST_PATH`` is unset, the committed ``watchlist.sample.txt`` is used and
the result is flagged ``is_sample`` so callers never prune against it.

File format (one CoinGecko id per line):

    # comments and blank lines are ignored
    bitcoin
    ethereum   # inline comments are fine
    "solana",  # lines pasted from a Python list are accepted too
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_PATH = Path(__file__).resolve().parent / "watchlist.sample.txt"

_TOKEN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class WatchlistError(ValueError):
    """Raised when the watchlist is missing, empty, or contains invalid lines."""


@dataclass(frozen=True)
class Watchlist:
    token_ids: list[str]
    source: str
    is_sample: bool
    duplicates: list[str] = field(default_factory=list)


def parse_watchlist(text: str, source: str = "<string>") -> tuple[list[str], list[str]]:
    """Return ``(token_ids, duplicates)`` in file order; raise on any invalid line."""
    token_ids: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    errors: list[str] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip().rstrip(",").strip().strip("\"'").strip()
        if not line:
            continue
        token_id = line.lower()
        if not _TOKEN_ID_RE.match(token_id):
            errors.append(f"{source}:{lineno}: invalid CoinGecko id {raw.strip()!r}")
            continue
        if token_id in seen:
            duplicates.append(token_id)
            continue
        seen.add(token_id)
        token_ids.append(token_id)

    if errors:
        raise WatchlistError("watchlist has invalid lines:\n  " + "\n  ".join(errors))
    if not token_ids:
        raise WatchlistError(f"watchlist {source} contains no token ids")
    return token_ids, duplicates


def load_watchlist(path: str | None = None) -> Watchlist:
    """Load from *path*, else ``WATCHLIST_PATH``, else the committed sample.

    A configured path that is missing or unreadable is an error; it never falls
    back to the sample, because pruning against the wrong list deletes history.
    """
    configured = path if path is not None else os.getenv("WATCHLIST_PATH", "").strip()
    if configured:
        target, is_sample = Path(configured), False
        if not target.is_file():
            raise WatchlistError(f"WATCHLIST_PATH {configured!r} does not exist or is not a file")
    else:
        target, is_sample = SAMPLE_PATH, True

    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise WatchlistError(f"cannot read watchlist {target}: {exc}") from exc

    token_ids, duplicates = parse_watchlist(text, source=str(target))
    return Watchlist(token_ids=token_ids, source=str(target), is_sample=is_sample, duplicates=duplicates)
