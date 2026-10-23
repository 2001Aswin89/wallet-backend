"""Milestone 2: what would Serializable cost? Same stampede (Rs 500 wallet, 100 concurrent Rs 10
transfers) run at READ COMMITTED (what the app uses) and at SERIALIZABLE with a retry loop on 40001.

    DATABASE_URL=... python -m scripts.serializable_cost       writes docs/performance/serializable_cost.md

Both use the identical transfer code (app/services/transfer.py, row locks included); only the
isolation level differs. Fraud rules are switched off for the run and restored afterwards.
"""
import random
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from psycopg import IsolationLevel
from psycopg.errors import SerializationFailure
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.errors import AppError
from scripts.break_it import make_wallet
from app.services.transfer import execute_transfer

OUT = Path(__file__).resolve().parent.parent / "docs" / "performance" / "serializable_cost.md"
REPEATS = 5


def one_run(level) -> dict:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as c:
        src, dst = make_wallet(c, 50_000), make_wallet(c, 0)
        uid = c.execute("SELECT user_id FROM accounts WHERE id = %s", (src,)).fetchone()["user_id"]
        c.commit()
    attempts, ok, short = [], [0], [0]
    lock = threading.Lock()

    def worker(_):
        n = 0
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            conn.isolation_level = level
            while True:
                n += 1
                try:
                    with conn.transaction():
                        execute_transfer(conn, kind="peer_transfer", caller_id=uid, wallet_id=src,
                                         other_wallet_id=dst, amount=1_000)
                    with lock:
                        ok[0] += 1
                    break
                except AppError:                                   # INSUFFICIENT_FUNDS: a final answer
                    with lock:
                        short[0] += 1
                    break
                except SerializationFailure:                       # 40001: retry the whole transaction
                    time.sleep(random.uniform(0, 0.005) * min(n, 10))
        with lock:
            attempts.append(n)

    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=100) as pool:
        list(pool.map(worker, range(100)))
    wall = time.perf_counter() - t
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as c:
        bal = c.execute("SELECT balance FROM accounts WHERE id = %s", (src,)).fetchone()["balance"]
    return {"wall": wall, "ok": ok[0], "short": short[0], "attempts": sum(attempts), "max_attempts": max(attempts),
            "final_balance": bal}


def main() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        before = c.execute("SELECT id, enabled FROM risk_rules").fetchall()
        c.execute("UPDATE risk_rules SET enabled = false")
    rows = {}
    try:
        for name, level in (("READ COMMITTED + row locks (the app)", IsolationLevel.READ_COMMITTED),
                            ("SERIALIZABLE + row locks + retry on 40001", IsolationLevel.SERIALIZABLE)):
            rows[name] = [one_run(level) for _ in range(REPEATS)]
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            for rid, en in before:
                c.execute("UPDATE risk_rules SET enabled = %s WHERE id = %s", (en, rid))
    out = ["# Serializable versus Read Committed on one hot wallet", "",
           f"100 concurrent Rs 10 transfers out of a Rs 500 wallet, {REPEATS} runs per level, one connection per "
           "thread. Correct outcome every time: 50 succeed, 50 INSUFFICIENT_FUNDS, final balance 0.", "",
           "| isolation level | median wall time (s) | transactions started (incl. retries) | 40001 retries | worst single request: attempts | all runs correct |",
           "|---|---|---|---|---|---|"]
    for name, rs in rows.items():
        correct = all(r["ok"] == 50 and r["short"] == 50 and r["final_balance"] == 0 for r in rs)
        out.append(f"| {name} | {statistics.median(r['wall'] for r in rs):.2f} | "
                   f"{statistics.median(r['attempts'] for r in rs):.0f} | "
                   f"{statistics.median(r['attempts'] - 100 for r in rs):.0f} | "
                   f"{max(r['max_attempts'] for r in rs)} | {correct} |")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    print(rows)


if __name__ == "__main__":
    main()
