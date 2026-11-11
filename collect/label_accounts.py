"""Look up every account with enough games in the Lichess public API and record its status.

The API marks accounts that broke the terms of service (cheating, boosting, sandbagging) with
`tosViolation`, and closed accounts with `disabled`. Both are public on the profile page.
Usernames never leave the machine this runs on: build_dataset.py replaces them with salted hashes.

    python -m collect.label_accounts --games work/games --out work/accounts.parquet
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

API = "https://lichess.org/api/users"
AGENT = "fair-play-detection (research project; github.com/satwik-kotta/fair-play-detection)"
BATCH = 300
FIELDS = ["id", "created_at", "seen_at", "tos_violation", "disabled", "title", "play_seconds", "found"]


def accounts_to_label(games: str, min_games: int) -> list[str]:
    q = f"""
        WITH p AS (
            SELECT lower(white) AS id, white_title AS title FROM read_parquet('{games}/*.parquet')
            UNION ALL
            SELECT lower(black), black_title FROM read_parquet('{games}/*.parquet')
        )
        SELECT id FROM p WHERE id IS NOT NULL GROUP BY id
        HAVING count(*) >= {min_games} AND coalesce(max(title), '') <> 'BOT' ORDER BY id"""
    return [r[0] for r in duckdb.sql(q).fetchall()]


def fetch(ids: list[str], opener=urllib.request.urlopen, pause=61):
    req = urllib.request.Request(API, data=",".join(ids).encode(), method="POST",
                                 headers={"Content-Type": "text/plain", "User-Agent": AGENT})
    for attempt in range(6):
        try:
            with opener(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:                                   # asked to slow down: wait a full minute
                time.sleep(pause)
            elif e.code >= 500:
                time.sleep(5 * (attempt + 1))
            else:
                raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Lichess API kept failing")


def to_row(u: dict):
    return (u["id"], u.get("createdAt"), u.get("seenAt"), bool(u.get("tosViolation", False)),
            bool(u.get("disabled", False)), u.get("title"), (u.get("playTime") or {}).get("total"), True)


def label(ids, fetcher=fetch, sleep=0.6, log=print):
    rows, seen = [], set()
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        for u in fetcher(chunk):
            rows.append(to_row(u))
            seen.add(u["id"])
        rows += [(x, None, None, False, False, None, None, False) for x in chunk if x not in seen]
        if (i // BATCH) % 100 == 0:
            log(f"{i + len(chunk):>9,} / {len(ids):,} accounts", flush=True)
        time.sleep(sleep)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--games", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--min-games", type=int, default=3)
    a = p.parse_args(argv)
    ids = accounts_to_label(a.games, a.min_games)
    print(f"{len(ids):,} accounts with at least {a.min_games} games", flush=True)
    rows = label(ids)
    cols = list(zip(*rows))
    pq.write_table(pa.table({n: pa.array(c) for n, c in zip(FIELDS, cols)}), a.out)
    n = len(rows)
    tos = sum(r[3] for r in rows); closed = sum(r[4] for r in rows); both = sum(r[3] and r[4] for r in rows)
    print(f"labelled {n:,}: found {sum(r[7] for r in rows):,}, tosViolation {tos:,} ({tos / n:.2%}), "
          f"closed {closed:,} ({closed / n:.2%}), both {both:,}")


if __name__ == "__main__":
    main()
