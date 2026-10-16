"""Milestone 2, Step 2 - BREAK IT FIRST. Deliberately UNSAFE transfer: read balance, check in
code, then write, with NO row lock. Never imported by the app.

Run:  python -m scripts.break_it        (needs DATABASE_URL; app migrations already applied)
Fires 100 parallel Rs 10 transfers out of a Rs 500 wallet and compares the outcome with the
ledger. The safe version (app/services/transfer.py) is compared in tests/test_stampede.py.
"""
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.migrate import run_migrations
from app.services.transfer import execute_transfer


class InsufficientFunds(Exception):
    pass


def transfer_unsafe(src: int, dst: int, amount: int) -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:   # READ COMMITTED, one tx
        bal = conn.execute("SELECT balance FROM accounts WHERE id = %s", (src,)).fetchone()["balance"]
        if bal < amount:                                                # check in code ...
            raise InsufficientFunds()
        dst_bal = conn.execute("SELECT balance FROM accounts WHERE id = %s", (dst,)).fetchone()["balance"]
        t = conn.execute(
            "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, completed_at) "
            "VALUES ('peer_transfer', 'completed', %s, %s, %s, now()) RETURNING id", (amount, src, dst)
        ).fetchone()
        conn.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
            "VALUES (%s, %s, 'debit', %s), (%s, %s, 'credit', %s)", (t["id"], src, amount, t["id"], dst, amount)
        )
        # ... then write an absolute value computed from the stale read: the LOST UPDATE.
        conn.execute("UPDATE accounts SET balance = %s WHERE id = %s", (bal - amount, src))
        conn.execute("UPDATE accounts SET balance = %s WHERE id = %s", (dst_bal + amount, dst))


def make_wallet(conn, funded: int) -> int:
    n = uuid.uuid4().int % 10**10
    uid = conn.execute(
        "INSERT INTO users (name, email, phone, kyc_status) VALUES ('Break', %s, %s, 'verified') RETURNING id",
        (f"b{uuid.uuid4().hex}@example.com", f"+91{n:010d}"),
    ).fetchone()["id"]
    wid = conn.execute(
        "INSERT INTO accounts (account_type, user_id, label) VALUES ('user_wallet', %s, 'main') RETURNING id",
        (uid,),
    ).fetchone()["id"]
    if funded:
        execute_transfer(conn, kind="top_up", caller_id=uid, wallet_id=wid, other_wallet_id=None, amount=funded)
    return wid


def attempt(src, dst):
    try:
        transfer_unsafe(src, dst, 1_000)
        return "ok"
    except InsufficientFunds:
        return "insufficient"


def main() -> None:
    run_migrations()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        src, dst = make_wallet(conn, 50_000), make_wallet(conn, 0)
    with ThreadPoolExecutor(max_workers=100) as pool:
        outcomes = list(pool.map(lambda _: attempt(src, dst), range(100)))
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        stored = conn.execute("SELECT balance FROM accounts WHERE id=%s", (src,)).fetchone()["balance"]
        stored_dst = conn.execute("SELECT balance FROM accounts WHERE id=%s", (dst,)).fetchone()["balance"]
        computed = conn.execute(
            "SELECT COALESCE(SUM(CASE direction WHEN 'credit' THEN amount ELSE -amount END),0) AS b "
            "FROM ledger_entries WHERE account_id=%s", (src,)
        ).fetchone()["b"]
        sent = conn.execute("SELECT count(*) AS n FROM transfers WHERE from_account_id=%s", (src,)).fetchone()["n"]
    print(f"requests that reported success      : {outcomes.count('ok')}   (correct answer: 50)")
    print(f"transfer rows actually recorded      : {sent}")
    print(f"source wallet stored balance (paise) : {stored}   (correct answer: 0)")
    print(f"source wallet balance from ledger    : {computed}   (the ledger always tells the truth)")
    print(f"destination stored balance (paise)   : {stored_dst}   (ledger says {outcomes.count('ok') * 1000})")
    print("LOST UPDATE DETECTED: stored balance != ledger" if stored != computed else
          "no divergence this run (race did not fire; run again)")


if __name__ == "__main__":
    sys.exit(main())
