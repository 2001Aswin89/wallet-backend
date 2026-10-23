"""Pagination: LIMIT/OFFSET versus keyset (cursor) on a large ledger.

    DATABASE_URL=...wallet_1m python -m scripts.measure_pagination

Uses the CASH_IN system account, which holds the most ledger entries after seeding. Writes
docs/performance/pagination.md with real EXPLAIN (ANALYZE, BUFFERS) output.
"""
import re
import statistics
import time
from pathlib import Path

import psycopg

from app.config import DATABASE_URL

OUT = Path(__file__).resolve().parent.parent / "docs" / "performance" / "pagination.md"
OFFSETS = [0, 1_000, 10_000, 50_000, 100_000]
BASE = ("SELECT le.id, le.transfer_id, le.direction, le.amount, le.created_at FROM ledger_entries le "
        "WHERE le.account_id = %(a)s ")
Q_OFFSET = BASE + "ORDER BY le.created_at DESC, le.id DESC LIMIT 50 OFFSET %(o)s"
Q_CURSOR = BASE + "AND (le.created_at, le.id) < (%(ts)s, %(id)s) ORDER BY le.created_at DESC, le.id DESC LIMIT 50"


def timed(conn, sql, params, runs=7) -> float:
    samples = []
    for _ in range(runs):
        t = time.perf_counter()
        conn.execute(sql, params).fetchall()
        samples.append((time.perf_counter() - t) * 1000)
    return statistics.median(samples)


def plan(conn, sql, params) -> str:
    return "\n".join(r[0] for r in conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params))


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        acct = conn.execute("SELECT id FROM accounts WHERE system_code = 'CASH_IN'").fetchone()[0]
        n = conn.execute("SELECT count(*) FROM ledger_entries WHERE account_id = %s", (acct,)).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
        assert n > max(OFFSETS) + 50, f"account has only {n} entries; seed more transfers"
        rows = []
        for o in OFFSETS:
            # the row the cursor would resume after, so both queries return the same page
            ts = rid = None
            if o:
                row = conn.execute(BASE + "ORDER BY le.created_at DESC, le.id DESC OFFSET %(o)s LIMIT 1",
                                   {"a": acct, "o": o - 1}).fetchone()
                ts, rid = row[4], row[0]
            same = None
            if o:
                a = conn.execute(Q_OFFSET, {"a": acct, "o": o}).fetchall()
                b = conn.execute(Q_CURSOR, {"a": acct, "ts": ts, "id": rid}).fetchall()
                same = a == b
            off_ms = timed(conn, Q_OFFSET, {"a": acct, "o": o})
            cur_ms = timed(conn, Q_CURSOR, {"a": acct, "ts": ts, "id": rid}) if o else None
            rows.append((o, off_ms, cur_ms, same))
        deep = max(OFFSETS)
        ts, rid = (lambda r: (r[4], r[0]))(conn.execute(BASE + "ORDER BY le.created_at DESC, le.id DESC OFFSET %(o)s LIMIT 1",
                                                        {"a": acct, "o": deep - 1}).fetchone())
        p_off = plan(conn, Q_OFFSET, {"a": acct, "o": deep})
        p_cur = plan(conn, Q_CURSOR, {"a": acct, "ts": ts, "id": rid})
    table = "| OFFSET | LIMIT/OFFSET (median ms) | cursor at same position (median ms) | same 50 rows |\n|---|---|---|---|\n"
    for o, off_ms, cur_ms, same in rows:
        table += f"| {o:,} | {off_ms:,.2f} | {'-' if cur_ms is None else f'{cur_ms:,.2f}'} | {'-' if same is None else same} |\n"
    out = (f"# Statement pagination at scale\n\nAccount: CASH_IN (id {acct}) with {n:,} entries; ledger has {total:,} "
           f"entries. Median of 7 runs per cell, warm cache.\n\n{table}\n"
           f"## EXPLAIN (ANALYZE, BUFFERS), OFFSET {deep:,}\n\n```text\n{p_off}\n```\n\n"
           f"## EXPLAIN (ANALYZE, BUFFERS), cursor resuming after that same row\n\n```text\n{p_cur}\n```\n")
    OUT.write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
