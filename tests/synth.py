"""Synthetic games in the exact text format of the Lichess dump, for tests and local dry runs.

Honest players win according to the Elo curve. Cheaters win far more than their rating says,
lose few centipawns and spend very even time on each move.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

OPENINGS = [("B01", "Scandinavian Defense"), ("C50", "Italian Game"), ("A45", "Indian Defense"), ("D00", "Queen's Pawn Game")]
SPEED = [("Bullet", 60, 0), ("Blitz", 180, 0), ("Blitz", 300, 3), ("Rapid", 600, 0)]


def clock(s):
    s = max(0, int(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def game_text(rng, gid, t, white, black, welo, belo, result, w_cheat=False, b_cheat=False, evals=False, bot=None):
    name, base, inc = rng.choice(SPEED)
    plies = rng.randint(30, 110)
    eco, opening = rng.choice(OPENINGS)
    clocks, ev, moves = [base, base], 0.2, []
    for ply in range(plies):
        side = ply % 2
        cheat = (w_cheat, b_cheat)[side]
        if ply >= 2:
            spend = rng.gauss(base / 55, base / 400) if cheat else rng.expovariate(1 / (base / 45))
            clocks[side] = max(1, clocks[side] - max(0.2, spend) + inc)
        loss = abs(rng.gauss(0, 0.08 if cheat else 0.6))
        ev = ev - loss if side == 0 else ev + loss
        note = (f"[%eval {max(-9.9, min(9.9, ev)):.2f}] " if evals else "") + f"[%clk {clock(clocks[side])}]"
        num = f"{ply // 2 + 1}." if side == 0 else f"{ply // 2 + 1}..."
        moves.append(f"{num} e4 {{ {note} }}")
    res = {1.0: "1-0", 0.0: "0-1", 0.5: "1/2-1/2"}[result]
    d = round(16 * (result - 1 / (1 + 10 ** ((belo - welo) / 400))))
    head = [("Event", f"Rated {name} game"), ("Site", f"https://lichess.org/{gid}"), ("Date", t.strftime("%Y.%m.%d")),
            ("Round", "-"), ("White", white), ("Black", black), ("Result", res), ("UTCDate", t.strftime("%Y.%m.%d")),
            ("UTCTime", t.strftime("%H:%M:%S")), ("WhiteElo", str(welo)), ("BlackElo", str(belo)),
            ("WhiteRatingDiff", f"{d:+d}"), ("BlackRatingDiff", f"{-d:+d}")]
    if bot == "w":
        head.append(("WhiteTitle", "BOT"))
    head += [("ECO", eco), ("Opening", opening), ("TimeControl", f"{base}+{inc}"), ("Termination", "Normal")]
    return "\n".join(f'[{k} "{v}"]' for k, v in head) + "\n\n" + " ".join(moves) + f" {res}\n"


def world(n_players=400, n_games=6000, cheat_share=0.05, seed=0, start=datetime(2026, 8, 1)):
    """Return (pgn text, {lowercase name: is cheater})."""
    rng = random.Random(seed)
    players = [(f"Player{i}", rng.gauss(1500, 250), rng.random() < cheat_share) for i in range(n_players)]
    elo = {p[0]: int(p[1]) for p in players}
    cheat = {p[0]: p[2] for p in players}
    out, t = [], start
    for g in range(n_games):
        t += timedelta(seconds=rng.randint(5, 120))
        a, b = rng.sample(players, 2)
        w, bl = a[0], b[0]
        expect = 1 / (1 + 10 ** ((elo[bl] - elo[w]) / 400))
        if cheat[w] and not cheat[bl]:
            expect = 0.93
        elif cheat[bl] and not cheat[w]:
            expect = 0.07
        r = rng.random()
        result = 1.0 if r < expect * 0.95 else (0.5 if r < expect * 0.95 + 0.05 else 0.0)
        out.append(game_text(rng, f"g{g:07d}", t, w, bl, elo[w], elo[bl], result, cheat[w], cheat[bl], evals=g % 8 == 0,
                             bot="w" if g % 997 == 0 else None))
        k = 16 * (result - 1 / (1 + 10 ** ((elo[bl] - elo[w]) / 400)))
        elo[w] += round(k)
        elo[bl] -= round(k)
    return "\n".join(out), {k.lower(): v for k, v in cheat.items()}


def fake_api(cheaters: dict, closed_share=0.02, seed=1):
    """A stand in for the Lichess users endpoint with the same response shape."""
    rng = random.Random(seed)

    def fetcher(ids):
        res = []
        for i in ids:
            if i not in cheaters:
                continue
            u = {"id": i, "username": i, "createdAt": 1785000000000 - rng.randint(0, 10 ** 10), "seenAt": 1790000000000,
                 "playTime": {"total": 1000}}
            if cheaters[i]:
                u["tosViolation"] = True
            if rng.random() < closed_share:
                u["disabled"] = True
            res.append(u)
        return res
    return fetcher
