"""Read the Lichess PGN dump from stdin and write Parquet parts of flat game rows.

    curl -sL https://database.lichess.org/standard/lichess_db_standard_rated_2026-08.pgn.zst \\
      | zstd -dc | python -m collect.stream_games --out work/games --max-games 15000000

The reader cuts the stream into blocks that end on a game boundary and hands them to worker
processes. It stops after --max-games, which closes the pipe and ends the download.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from multiprocessing import Pool
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .parse import SCHEMA, parse_block

BLOCK_BYTES = 48 * 1024 * 1024
BOUNDARY = b"\n\n[Event "
STR = ["game_id", "ts", "white", "black", "white_title", "black_title", "speed", "termination", "eco"]
INT = ["white_elo", "black_elo", "white_diff", "black_diff", "base", "inc", "plies"]
TYPES = {**{c: pa.string() for c in STR}, **{c: pa.int32() for c in INT}, "tournament": pa.bool_()}   # the rest are floats


def blocks(stream, limit_bytes=BLOCK_BYTES):
    """Yield chunks of the stream that each end right before a game's first header."""
    carry = b""
    while True:
        data = stream.read(limit_bytes)
        if not data:
            if carry.strip():
                yield carry
            return
        data = carry + data
        cut = data.rfind(BOUNDARY)
        if cut <= 0:
            carry = data
            continue
        yield data[:cut + 2]
        carry = data[cut + 2:]


def work(args):
    i, raw, out = args
    rows = parse_block(raw.decode("utf-8", "replace"))
    if rows:
        cols = list(zip(*rows))
        table = pa.table({n: pa.array(c, type=TYPES.get(n, pa.float32())) for n, c in zip(SCHEMA, cols)})
        pq.write_table(table, f"{out}/part-{i:05d}.parquet", compression="zstd")
    return len(rows)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--max-games", type=int, default=15_000_000)
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args(argv)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    total, t0 = 0, time.time()

    def tally(n):
        nonlocal total
        before, total = total, total + n
        if total // 500_000 != before // 500_000:
            print(f"{total:>11,} games  {time.time() - t0:6.0f}s", flush=True)

    # Bounded hand off: never hold more than a few blocks in memory, and stop reading once enough games are in.
    with Pool(a.workers) as pool:
        pending = deque()
        for i, raw in enumerate(blocks(sys.stdin.buffer)):
            pending.append(pool.apply_async(work, ((i, raw, a.out),)))
            while len(pending) >= a.workers * 2:
                tally(pending.popleft().get())
            if total >= a.max_games:
                break
        while pending:
            tally(pending.popleft().get())
    print(f"done: {total:,} games in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
