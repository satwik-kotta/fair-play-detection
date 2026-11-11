"""Parse games from the Lichess monthly PGN dump into flat rows.

One row per game with the header fields, plus per side summaries of the move text:
move times from the clock comments and average centipawn loss where the game carries
engine evaluations (about one game in ten).
"""
from __future__ import annotations

import re

CLK = re.compile(r"%clk (\d+):(\d\d):(\d\d)")
EVAL = re.compile(r"%eval (#?-?[\d.]+)")
HEADER = re.compile(r'^\[(\w+) "(.*)"\]$')
RESULT = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}
CP_CAP = 1000          # evaluations are capped at 10 pawns, mates count as the cap
FAST = 2.0             # seconds: a move this quick is instant
SCHEMA = [
    "game_id", "ts", "white", "black", "result", "white_elo", "black_elo", "white_diff", "black_diff",
    "white_title", "black_title", "speed", "base", "inc", "tournament", "termination", "eco", "plies",
    "w_mt_mean", "w_mt_cv", "w_mt_fast", "b_mt_mean", "b_mt_cv", "b_mt_fast", "w_acpl", "b_acpl",
]


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def speed_of(event: str) -> str:
    e = event.lower()
    for s in ("ultrabullet", "bullet", "blitz", "rapid", "classical", "correspondence"):
        if s in e:
            return s
    return "other"


def move_time_stats(clocks: list[int], inc: int):
    """Mean, coefficient of variation and share of instant moves for one side's clock readings."""
    if len(clocks) < 6:
        return None, None, None
    times = [max(0.0, clocks[i - 1] - clocks[i] + inc) for i in range(1, len(clocks))]
    n = len(times)
    mean = sum(times) / n
    var = sum((t - mean) ** 2 for t in times) / n
    return mean, (var ** 0.5 / mean if mean > 0 else 0.0), sum(t <= FAST for t in times) / n


def _cp(token: str) -> float:
    if token[0] == "#":
        return -CP_CAP if "-" in token else CP_CAP
    return max(-CP_CAP, min(CP_CAP, float(token) * 100))


def acpl(evals: list[float]):
    """Average centipawn loss per side from the evaluation after every ply (white's point of view)."""
    if len(evals) < 20:
        return None, None
    loss = {0: [], 1: []}
    prev = 15.0                                   # the starting position is worth about 0.15 for white
    for ply, e in enumerate(evals):
        side = ply % 2                            # 0 = white just moved
        drop = (prev - e) if side == 0 else (e - prev)
        loss[side].append(max(0.0, drop))
        prev = e
    return sum(loss[0]) / len(loss[0]), sum(loss[1]) / len(loss[1])


def parse_game(h: dict, moves: str):
    result = RESULT.get(h.get("Result"))
    site = h.get("Site", "")
    if result is None or not site:
        return None
    tc = h.get("TimeControl", "-")
    base, _, inc = tc.partition("+")
    base, inc = _int(base), _int(inc) or 0
    event = h.get("Event", "")
    clocks = [int(a) * 3600 + int(b) * 60 + int(c) for a, b, c in CLK.findall(moves)]
    plies = len(clocks) if clocks else moves.count(". ") + moves.count("... ")
    w = move_time_stats(clocks[0::2], inc) if clocks else (None, None, None)
    b = move_time_stats(clocks[1::2], inc) if clocks else (None, None, None)
    wa, ba = acpl([_cp(t) for t in EVAL.findall(moves)]) if "%eval" in moves else (None, None)
    return (
        site.rsplit("/", 1)[-1],
        f'{h.get("UTCDate", "").replace(".", "-")} {h.get("UTCTime", "")}',
        h.get("White"), h.get("Black"), result,
        _int(h.get("WhiteElo")), _int(h.get("BlackElo")),
        _int(h.get("WhiteRatingDiff")), _int(h.get("BlackRatingDiff")),
        h.get("WhiteTitle"), h.get("BlackTitle"),
        speed_of(event), base, inc, "tournament" in event.lower() or "swiss" in event.lower(),
        h.get("Termination"), h.get("ECO"), plies,
        *w, *b, wa, ba,
    )


def parse_block(text: str):
    """Parse a block of PGN text holding whole games. Returns a list of rows."""
    rows, h, moves = [], {}, None
    for line in text.split("\n"):
        if not line:
            continue
        if line[0] == "[":
            if moves is not None:                 # a header after move text starts the next game
                row = parse_game(h, moves)
                if row:
                    rows.append(row)
                h, moves = {}, None
            m = HEADER.match(line)
            if m:
                h[m.group(1)] = m.group(2)
        else:
            moves = line if moves is None else moves + " " + line
    if h and moves is not None:
        row = parse_game(h, moves)
        if row:
            rows.append(row)
    return rows
