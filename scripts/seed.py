"""Seed internally consistent data fast: COPY, never the HTTP API.

    python -m scripts.seed --users 1000 --wallets 2 --transfers 100000

Transfers are generated in memory while tracking every wallet balance, so no wallet ever goes
negative; ledger entries are written in balanced debit/credit pairs, and stored balances are set
from the tracked values at the end. Reconciliation on a fresh seed must pass clean.

Commits once per batch so the deferred balanced-ledger trigger does not queue millions of events.
"""
import argparse
import random
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

FIRST_NAMES = ["Rahul", "Asha", "Priya", "Vikram", "Neha", "Arjun", "Sneha", "Karthik", "Meera", "Rohan",
               "Divya", "Amit", "Pooja", "Suresh", "Anita", "Manoj", "Kavya", "Sanjay", "Ritu", "Deepak"]
LAST_NAMES = ["Sharma", "Iyer", "Patel", "Nair", "Gupta", "Reddy", "Singh", "Menon", "Das", "Joshi"]
NOTES = ["rent", "dinner", "movie", "groceries", "gift", "cab", "split bill", "loan repayment", None, None, None]


def seed(dsn: str | None = None, *, users: int = 1000, wallets_per_user: int = 2, transfers: int = 100_000,
         batch: int = 10_000, rng_seed: int = 42, days: int = 90) -> dict:
    dsn = dsn or DATABASE_URL
    rng = random.Random(rng_seed)
    started = time.monotonic()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        cash_in, cash_out = (conn.execute("SELECT id FROM accounts WHERE system_code = %s", (c,)).fetchone()["id"]
                             for c in ("CASH_IN", "CASH_OUT"))
        first_user = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM users").fetchone()["m"]
        tag = f"{int(time.time()) % 10**8:08d}"

        with conn.cursor() as cur, cur.copy("COPY users (name, email, phone, kyc_status) FROM STDIN") as cp:
            for i in range(users):
                name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
                cp.write_row((name, f"seed{tag}.{i}@example.com", f"+91{6_000_000_000 + (int(tag) * 1000 + i) % 3_999_999_999}",
                              "verified"))
        user_ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE id > %s ORDER BY id", (first_user,))]
        first_acct = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM accounts").fetchone()["m"]
        labels = ["main", "savings"] + [f"wallet{i}" for i in range(3, wallets_per_user + 1)]
        with conn.cursor() as cur, cur.copy("COPY accounts (account_type, user_id, label) FROM STDIN") as cp:
            for uid in user_ids:
                for label in labels[:wallets_per_user]:
                    cp.write_row(("user_wallet", uid, label))
        wallets = [(r["id"], r["user_id"]) for r in conn.execute(
            "SELECT id, user_id FROM accounts WHERE id > %s AND account_type = 'user_wallet' ORDER BY id", (first_acct,))]
        conn.commit()

        owner = dict(wallets)
        wallet_ids = [w for w, _ in wallets]
        balance = {w: 0 for w in wallet_ids}
        balance[cash_in] = balance[cash_out] = 0
        start = datetime.now(timezone.utc) - timedelta(days=days)
        step = timedelta(days=days).total_seconds() / max(transfers, 1)

        made = 0
        counts = {"top_up": 0, "peer_transfer": 0, "withdrawal": 0}
        while made < transfers:
            n = min(batch, transfers - made)
            rows = []
            for k in range(n):
                ts = start + timedelta(seconds=(made + k) * step)
                roll = rng.random()
                src = rng.choice(wallet_ids)
                if roll >= 0.35 and balance[src] >= 100:
                    cap = min(balance[src], 50_000)
                    amount = rng.randint(100, cap)
                    if roll < 0.85:
                        dst = rng.choice(wallet_ids)
                        while dst == src:
                            dst = rng.choice(wallet_ids)
                        kind, frm, to, uid, note = "peer_transfer", src, dst, owner[src], rng.choice(NOTES)
                    else:
                        kind, frm, to, uid, note = "withdrawal", src, cash_out, owner[src], None
                else:
                    amount = rng.randint(10_000, 200_000)
                    kind, frm, to, uid, note = "top_up", cash_in, src, owner[src], None
                balance[frm] -= amount
                balance[to] += amount
                counts[kind] += 1
                rows.append((kind, amount, frm, to, uid, note, ts))

            prev_max = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM transfers").fetchone()["m"]
            with conn.cursor() as cur, cur.copy(
                    "COPY transfers (type, status, amount, from_account_id, to_account_id, initiated_by, note, "
                    "created_at, completed_at) FROM STDIN") as cp:
                for kind, amount, frm, to, uid, note, ts in rows:
                    cp.write_row((kind, "completed", amount, frm, to, uid, note, ts, ts))
            ids = [r["id"] for r in conn.execute("SELECT id FROM transfers WHERE id > %s ORDER BY id", (prev_max,))]
            assert len(ids) == len(rows), "unexpected concurrent writes while seeding"
            with conn.cursor() as cur, cur.copy(
                    "COPY ledger_entries (transfer_id, account_id, direction, amount, created_at) FROM STDIN") as cp:
                for tid, (kind, amount, frm, to, uid, note, ts) in zip(ids, rows):
                    cp.write_row((tid, frm, "debit", amount, ts))
                    cp.write_row((tid, to, "credit", amount, ts))
            conn.commit()
            made += n

        with conn.cursor() as cur:
            cur.executemany("UPDATE accounts SET balance = %s WHERE id = %s", [(b, a) for a, b in balance.items()])
        conn.commit()
    return {"users": users, "wallets": len(wallets), "transfers": transfers, "entries": transfers * 2,
            "by_type": counts, "seconds": round(time.monotonic() - started, 1)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--users", type=int, default=1000)
    p.add_argument("--wallets", type=int, default=2)
    p.add_argument("--transfers", type=int, default=100_000)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()
    print(seed(users=a.users, wallets_per_user=a.wallets, transfers=a.transfers, rng_seed=a.seed))


if __name__ == "__main__":
    main()
