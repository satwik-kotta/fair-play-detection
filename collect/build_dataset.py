"""Join games to account labels and write the hashed datasets that are committed to the repository.

Every username is replaced by the first 16 hex digits of sha256(salt + name). The salt is random,
made here and never written anywhere, so the hashes cannot be turned back into names or matched
to another run.

Outputs (in --out):
  accounts.parquet       one row per labelled account
  sequences.parquet      game by game history, in order, for every flagged account and a sample of the rest
  encounters.parquet     games seen from an honest player's side: every game against a flagged opponent
                         and a sample of games against clean opponents, with what the player did next
  summary.json           totals and checks

    python -m collect.build_dataset --games work/games --accounts work/accounts.parquet --out data
"""
from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

import duckdb

SPEEDS = ("ultrabullet", "bullet", "blitz", "rapid", "classical")
MAX_SEQ = 40             # games kept per account in sequences.parquet
CLEAN_ACCOUNT_SHARE = 8  # percent of unflagged accounts kept in sequences.parquet
CLEAN_GAME_SHARE = 4     # percent of games against clean opponents kept in encounters.parquet


def build(games: str, accounts: str, out: str, salt: str | None = None, clean_account_share=CLEAN_ACCOUNT_SHARE,
          clean_game_share=CLEAN_GAME_SHARE):
    salt = salt or secrets.token_hex(16)
    Path(out).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET memory_limit = '10GB'")
    con.execute("SET temp_directory = 'duckdb_tmp'")
    h = lambda col: f"substr(sha256('{salt}' || lower({col})), 1, 16)"

    con.execute(f"""
        CREATE TABLE acct AS
        SELECT id, {h('id')} AS account, found, tos_violation AS flagged, disabled AS closed,
               to_timestamp(created_at / 1000) AT TIME ZONE 'UTC' AS created_at, title
        FROM read_parquet('{accounts}')""")

    # One row per player per game, humans only, real time speeds only.
    con.execute(f"""
        CREATE TABLE pg AS
        WITH g AS (
            SELECT *, strptime(ts, '%Y-%m-%d %H:%M:%S') AS t FROM read_parquet('{games}/*.parquet')
            WHERE speed IN {SPEEDS} AND white_elo IS NOT NULL AND black_elo IS NOT NULL
              AND coalesce(white_title, '') <> 'BOT' AND coalesce(black_title, '') <> 'BOT'
        )
        SELECT game_id, t, lower(white) AS player, lower(black) AS opp, 'w' AS color, result AS score,
               white_elo AS elo, black_elo AS opp_elo, white_diff AS diff, speed, base, inc, tournament, termination,
               plies, w_mt_mean AS mt_mean, w_mt_cv AS mt_cv, w_mt_fast AS mt_fast, w_acpl AS acpl, b_acpl AS opp_acpl
        FROM g
        UNION ALL
        SELECT game_id, t, lower(black), lower(white), 'b', 1 - result,
               black_elo, white_elo, black_diff, speed, base, inc, tournament, termination,
               plies, b_mt_mean, b_mt_cv, b_mt_fast, b_acpl, w_acpl
        FROM g""")
    t0, t1 = con.execute("SELECT min(t), max(t) FROM pg").fetchone()

    con.execute("""
        CREATE TABLE pgx AS
        SELECT pg.*,
               row_number() OVER w AS game_no,
               lead(t) OVER w AS next_t,
               count(*) OVER (PARTITION BY player) AS games_in_window
        FROM pg
        WINDOW w AS (PARTITION BY player ORDER BY t, game_id)""")

    con.execute(f"""
        COPY (
            SELECT a.account, a.flagged, a.closed, a.found, a.created_at, a.title IS NOT NULL AS titled,
                   s.games, s.first_t, s.last_t, s.first_elo, s.last_elo, s.wins, s.opponents
            FROM acct a
            JOIN (SELECT player, count(*) AS games, min(t) AS first_t, max(t) AS last_t,
                         arg_min(elo, t) AS first_elo, arg_max(elo, t) AS last_elo,
                         sum(CASE WHEN score = 1 THEN 1 ELSE 0 END) AS wins, count(DISTINCT opp) AS opponents
                  FROM pg GROUP BY player) s ON s.player = a.id
            ORDER BY a.account
        ) TO '{out}/accounts.parquet' (FORMAT parquet, COMPRESSION zstd)""")

    # Game histories: every flagged account, plus a fixed share of the others chosen by their hash.
    con.execute(f"""
        COPY (
            SELECT a.account, a.flagged, a.closed, a.created_at, x.game_no, x.t, x.color, x.score, x.elo, x.opp_elo, x.diff,
                   x.speed, x.base, x.inc, x.tournament, x.termination, x.plies, x.mt_mean, x.mt_cv, x.mt_fast,
                   x.acpl, x.opp_acpl, x.games_in_window, {h('x.opp')} AS opponent,
                   CASE WHEN a.flagged THEN 1.0 ELSE 100.0 / {clean_account_share} END AS weight
            FROM pgx x JOIN acct a ON a.id = x.player
            WHERE a.found AND x.game_no <= {MAX_SEQ}
              AND (a.flagged OR hash(a.account) % 100 < {clean_account_share})
            ORDER BY a.account, x.game_no
        ) TO '{out}/sequences.parquet' (FORMAT parquet, COMPRESSION zstd)""")

    # Encounters: an honest, open account's game against a labelled opponent, and what the player did next.
    con.execute(f"""
        COPY (
            SELECT p.account AS player, o.flagged AS opp_flagged, x.t, x.score, x.elo, x.opp_elo, x.diff, x.speed,
                   x.tournament, x.termination, x.plies, x.game_no, x.games_in_window, x.opp_acpl,
                   epoch(x.next_t) - epoch(x.t) AS secs_to_next,
                   epoch(TIMESTAMP '{t1}') - epoch(x.t) AS secs_to_end,
                   p.created_at AS player_created_at,
                   CASE WHEN o.flagged THEN 1.0 ELSE 100.0 / {clean_game_share} END AS weight
            FROM pgx x
            JOIN acct p ON p.id = x.player AND p.found AND NOT p.flagged AND NOT p.closed
            JOIN acct o ON o.id = x.opp AND o.found
            WHERE o.flagged OR hash(x.game_id || x.color) % 100 < {clean_game_share}
            ORDER BY x.t
        ) TO '{out}/encounters.parquet' (FORMAT parquet, COMPRESSION zstd)""")

    one = lambda q: con.execute(q).fetchone()
    n_games, with_clock, with_eval = one("""SELECT count(*), sum(CASE WHEN mt_mean IS NOT NULL THEN 1 ELSE 0 END),
                                                   sum(CASE WHEN acpl IS NOT NULL THEN 1 ELSE 0 END) FROM pg WHERE color = 'w'""")
    flags = con.execute("""SELECT found, flagged, closed, count(*) FROM acct GROUP BY ALL ORDER BY ALL""").fetchall()
    vs = one("""SELECT count(*), count(DISTINCT x.player), sum(CASE WHEN x.diff < 0 THEN -x.diff ELSE 0 END),
                       sum(CASE WHEN x.score = 0 THEN 1 ELSE 0 END)
                FROM pg x JOIN acct p ON p.id = x.player AND p.found AND NOT p.flagged AND NOT p.closed
                JOIN acct o ON o.id = x.opp AND o.flagged""")
    by_speed = con.execute("SELECT speed, count(*) FROM pg WHERE color = 'w' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    summary = {
        "window": {"first_game": str(t0), "last_game": str(t1)},
        "games": n_games, "games_with_clock": with_clock, "games_with_eval": with_eval,
        "games_by_speed": dict(by_speed),
        "players_seen": one("SELECT count(DISTINCT player) FROM pg")[0],
        "accounts_labelled": one("SELECT count(*) FROM acct")[0],
        "account_status": [{"found": a, "flagged": b, "closed": c, "accounts": n} for a, b, c, n in flags],
        "games_by_honest_players_against_flagged": vs[0], "honest_players_who_met_a_flagged_account": vs[1],
        "rating_points_lost_to_flagged": vs[2], "losses_to_flagged": vs[3],
        "sampling": {"clean_account_percent": clean_account_share, "clean_game_percent": clean_game_share,
                     "max_games_per_account": MAX_SEQ},
        "rows": {n: one(f"SELECT count(*) FROM read_parquet('{out}/{n}.parquet')")[0]
                 for n in ("accounts", "sequences", "encounters")},
    }
    Path(out, "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))
    return summary


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--games", required=True)
    p.add_argument("--accounts", required=True)
    p.add_argument("--out", default="data")
    a = p.parse_args(argv)
    build(a.games, a.accounts, a.out)


if __name__ == "__main__":
    main()
