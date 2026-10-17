"""Milestone 4 acceptance helper: corrupt the books directly in the database, exactly three ways.

    1. change one entry's amount        2. delete one credit entry        3. change one stored balance

Needs a superuser, because it bypasses the ledger's own append-only trigger for ONE session with
SET session_replication_role = replica. The triggers are never weakened for anyone else.
"""
import sys

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL


def corrupt(dsn: str | None = None) -> dict:
    with psycopg.connect(dsn or DATABASE_URL, row_factory=dict_row) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM transfers WHERE status = 'completed' AND type = 'peer_transfer' ORDER BY id")]
        t1, t2 = ids[len(ids) // 3], ids[2 * len(ids) // 3]
        touched = {r["account_id"] for r in conn.execute(
            "SELECT account_id FROM ledger_entries WHERE transfer_id = ANY(%s)", ([t1, t2],))}
        victim = conn.execute(
            "SELECT id FROM accounts WHERE account_type = 'user_wallet' AND id <> ALL(%s) ORDER BY id "
            "LIMIT 1", (list(touched),)).fetchone()["id"]
        e1 = conn.execute("SELECT id FROM ledger_entries WHERE transfer_id = %s AND direction = 'credit'",
                          (t1,)).fetchone()["id"]
        e2 = conn.execute("SELECT id FROM ledger_entries WHERE transfer_id = %s AND direction = 'credit'",
                          (t2,)).fetchone()["id"]

        conn.execute("SET session_replication_role = replica")            # this session only
        conn.execute("UPDATE ledger_entries SET amount = amount + 100 WHERE id = %s", (e1,))   # 1. changed amount
        conn.execute("DELETE FROM ledger_entries WHERE id = %s", (e2,))                         # 2. deleted credit
        conn.execute("UPDATE accounts SET balance = balance + 777 WHERE id = %s", (victim,))   # 3. bad stored balance
        conn.execute("SET session_replication_role = DEFAULT")
        conn.commit()
    return {"changed_amount_entry": e1, "changed_amount_transfer": t1,
            "deleted_credit_entry": e2, "deleted_credit_transfer": t2, "bad_balance_account": victim}


if __name__ == "__main__":
    print(corrupt())
    sys.exit(0)
