"""Milestone 2, Step 1: the two-session isolation lab.

    python -m scripts.isolation_lab            # writes docs/isolation.md

Two real PostgreSQL connections (session A and session B) run the four cases against a scratch
database built from the project's own migrations. Every command is printed in a psql-like
transcript and the conclusions are computed from the ACTUAL results, so the document cannot
claim something the database did not do.
"""
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.config import DATABASE_URL
from app.migrate import run_migrations

LAB_DB = "wallet_lab"
OUT = Path(__file__).resolve().parent.parent / "docs" / "isolation.md"


class Session:
    def __init__(self, name: str, dsn: str, transcript: list[str]):
        self.name, self.t = name, transcript
        self.conn = psycopg.connect(dsn, autocommit=True)   # we send BEGIN/COMMIT ourselves, like psql

    def run(self, sql: str):
        """Run one command; log it and its result. Returns (rows | None, error | None)."""
        self.t.append(f"{self.name}> {sql}")
        try:
            cur = self.conn.execute(sql)
            if cur.description:
                rows = cur.fetchall()
                header = [d.name for d in cur.description]
                widths = [max(len(h), *(len(str(r[i])) for r in rows)) if rows else len(h) for i, h in enumerate(header)]
                fmt = lambda cells: " " + " | ".join(str(c).rjust(w) for c, w in zip(cells, widths))
                self.t.append(fmt(header))
                self.t.append("-" + "-+-".join("-" * w for w in widths) + "-")
                self.t.extend(fmt(r) for r in rows)
                self.t.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
                return rows, None
            self.t.append(cur.statusmessage or "")
            return None, None
        except psycopg.Error as e:
            self.t.append(f"ERROR:  {e.diag.message_primary}")
            if e.diag.message_detail:
                self.t.append(f"DETAIL:  {e.diag.message_detail}")
            if e.diag.message_hint:
                self.t.append(f"HINT:  {e.diag.message_hint}")
            self.t.append(f"-- SQLSTATE {e.sqlstate}")
            try:
                self.conn.execute("ROLLBACK")
                self.t.append("-- (session rolled back)")
            except psycopg.Error:
                pass
            return None, e

    def note(self, text: str):
        self.t.append(f"-- {text}")


def reset(admin, balance=1000):
    admin.execute("UPDATE accounts SET balance = %s WHERE id IN (4, 5)", (balance,))


def main() -> None:
    info = conninfo_to_dict(DATABASE_URL)
    with psycopg.connect(DATABASE_URL, autocommit=True) as root:
        root.execute(f'DROP DATABASE IF EXISTS "{LAB_DB}" WITH (FORCE)')
        root.execute(f'CREATE DATABASE "{LAB_DB}"')
    info["dbname"] = LAB_DB
    dsn = make_conninfo(**info)
    run_migrations(dsn)
    admin = psycopg.connect(dsn, autocommit=True)
    admin.execute("INSERT INTO users (name, email, phone, kyc_status) VALUES ('Asha', 'asha@lab.test', '+919000000001', 'verified')")
    admin.execute("INSERT INTO accounts (account_type, user_id, label) VALUES ('user_wallet', 1, 'main'), ('user_wallet', 1, 'savings')")
    ids = [r[0] for r in admin.execute("SELECT id FROM accounts WHERE account_type = 'user_wallet' ORDER BY id")]
    assert ids == [4, 5], ids

    def topup(conn, account, amount):
        """A real top-up: transfer row + balanced entries + stored balance."""
        t = conn.execute("INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, completed_at) "
                         "VALUES ('top_up', 'completed', %s, 1, %s, now()) RETURNING id", (amount, account)).fetchone()[0]
        conn.execute("INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
                     "VALUES (%s, 1, 'debit', %s), (%s, %s, 'credit', %s)", (t, amount, t, account, amount))
        conn.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (amount, account))

    sections, facts = [], {}

    # ---------------------------------------------------------------- case 1: READ COMMITTED
    reset(admin)
    tr: list[str] = []
    a, b = Session("A", dsn, tr), Session("B", dsn, tr)
    a.run("BEGIN;")
    r1, _ = a.run("SELECT balance FROM accounts WHERE id = 4;")
    b.note("session B changes the row and commits")
    b.run("BEGIN;"); b.run("UPDATE accounts SET balance = 500 WHERE id = 4;"); b.run("COMMIT;")
    r2, _ = a.run("SELECT balance FROM accounts WHERE id = 4;")
    a.run("COMMIT;")
    facts["rc"] = (r1[0][0], r2[0][0])
    sections.append(("Case 1 - Read Committed (the PostgreSQL default)", tr))
    a.conn.close(); b.conn.close()

    # ---------------------------------------------------------------- case 2: REPEATABLE READ
    reset(admin)
    tr = []
    a, b = Session("A", dsn, tr), Session("B", dsn, tr)
    a.run("BEGIN ISOLATION LEVEL REPEATABLE READ;")
    r1, _ = a.run("SELECT balance FROM accounts WHERE id = 4;")
    b.note("session B changes the row and commits")
    b.run("BEGIN;"); b.run("UPDATE accounts SET balance = 500 WHERE id = 4;"); b.run("COMMIT;")
    r2, _ = a.run("SELECT balance FROM accounts WHERE id = 4;")
    a.note("A now tries to write based on what it saw (1000 - 10):")
    _, err = a.run("UPDATE accounts SET balance = balance - 10 WHERE id = 4;")
    facts["rr"] = (r1[0][0], r2[0][0], err.sqlstate if err else None, err.diag.message_primary if err else None)
    sections.append(("Case 2 - Repeatable Read", tr))
    a.conn.close(); b.conn.close()

    # ---------------------------------------------------------------- case 3: SERIALIZABLE (write skew)
    reset(admin)
    tr = []
    a, b = Session("A", dsn, tr), Session("B", dsn, tr)
    a.note("rule both sessions try to keep: the user's wallets together hold >= 1000")
    a.run("BEGIN ISOLATION LEVEL SERIALIZABLE;"); b.run("BEGIN ISOLATION LEVEL SERIALIZABLE;")
    sa, _ = a.run("SELECT sum(balance) FROM accounts WHERE user_id = 1;")
    sb, _ = b.run("SELECT sum(balance) FROM accounts WHERE user_id = 1;")
    a.note("each sees 2000, so each thinks it may take 600 from a DIFFERENT wallet")
    a.run("UPDATE accounts SET balance = balance - 600 WHERE id = 4;")
    b.run("UPDATE accounts SET balance = balance - 600 WHERE id = 5;")
    _, ea = a.run("COMMIT;")
    _, eb = b.run("COMMIT;")
    final, _ = Session("A", dsn, tr).run("SELECT id, balance FROM accounts WHERE id IN (4, 5) ORDER BY id;")
    errs = [e for e in (ea, eb) if e]
    facts["ser"] = (sa[0][0], sb[0][0], [(e.sqlstate, e.diag.message_primary) for e in errs], final)
    sections.append(("Case 3 - Serializable (write skew)", tr))
    a.conn.close(); b.conn.close()

    # ---------------------------------------------------------------- case 4: REPEATABLE READ with a join
    reset(admin, 0)
    admin.execute("DELETE FROM ledger_entries WHERE false")  # no-op; ledger is append-only anyway
    with admin.transaction():
        topup(admin, 4, 1000)
    join = ("SELECT a.id, a.balance, count(le.id) AS entries FROM accounts a "
            "JOIN ledger_entries le ON le.account_id = a.id WHERE a.id = 4 GROUP BY a.id, a.balance;")
    tr = []
    a, b = Session("A", dsn, tr), Session("B", dsn, tr)
    a.run("BEGIN ISOLATION LEVEL REPEATABLE READ;")
    j1, _ = a.run(join)
    p1, _ = a.run("SELECT count(*) AS phantom_check FROM ledger_entries WHERE account_id = 4;")
    b.note("session B adds a whole new ledger entry (a would-be phantom row) and updates the balance, then commits")
    b.run("BEGIN;")
    b.run("INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, completed_at) "
          "VALUES ('top_up', 'completed', 500, 1, 4, now());")
    b.run("INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
          "SELECT max(id), 1, 'debit', 500 FROM transfers UNION ALL SELECT max(id), 4, 'credit', 500 FROM transfers;")
    b.run("UPDATE accounts SET balance = balance + 500 WHERE id = 4;")
    b.run("COMMIT;")
    j2, _ = a.run(join)
    p2, _ = a.run("SELECT count(*) AS phantom_check FROM ledger_entries WHERE account_id = 4;")
    a.run("COMMIT;")
    j3, _ = a.run(join)
    facts["join"] = (j1[0], j2[0], p1[0][0], p2[0][0], j3[0])
    sections.append(("Case 4 - Repeatable Read with a join", tr))
    a.conn.close(); b.conn.close()
    admin.close()

    # ---------------------------------------------------------------- document
    rc, rr, ser, jn = facts["rc"], facts["rr"], facts["ser"], facts["join"]
    answers = {
        "Case 1 - Read Committed (the PostgreSQL default)":
            f"**Does A see a different value? {'Yes' if rc[0] != rc[1] else 'No'}.** A read {rc[0]}, then {rc[1]} "
            "inside the same transaction. Each statement gets a fresh snapshot, so a committed change by B shows up "
            "(a non-repeatable read). This is why a plain read-then-write is unsafe: the value you checked may be stale "
            "by the time you write.",
        "Case 2 - Repeatable Read":
            f"**What does A see now?** The same value both times ({rr[0]} then {rr[1]}): the snapshot is fixed at A's "
            f"first query. But when A tried to *write* a row B had already changed, PostgreSQL refused with SQLSTATE "
            f"`{rr[2]}` (\"{rr[3]}\"). A must roll back and retry. Repeatable Read never lets you silently overwrite "
            "a change you could not see.",
        "Case 3 - Serializable (write skew)":
            f"Both sessions read the same total ({ser[0]} and {ser[1]}) and each wrote a different row, which Repeatable "
            f"Read would allow (write skew). **Under Serializable, {len(ser[2])} of the two transactions was aborted:** "
            + "; ".join(f"SQLSTATE `{c}` \"{m}\"" for c, m in ser[2]) +
            f". Final balances: {ser[3]}. Serializable keeps the result equal to *some* one-at-a-time order, at the price "
            "that the application must retry transactions that fail with `40001`.",
        "Case 4 - Repeatable Read with a join":
            f"**Does the guarantee still hold? {'Yes' if jn[0] == jn[1] else 'No'}.** The join returned "
            f"`{tuple(jn[0])}` before B committed and `{tuple(jn[1])}` after, in the same transaction. The snapshot covers "
            f"every table the transaction reads, so the new ledger row did not appear either (count {jn[2]} then {jn[3]}): "
            f"no phantom. After A committed, a fresh read shows `{tuple(jn[4])}`. (PostgreSQL's Repeatable Read is stronger "
            "than the SQL standard requires, which allows phantoms at this level.)",
    }
    out = [
        "# Isolation lab (Milestone 2, Step 1)",
        "",
        f"Generated by `python -m scripts.isolation_lab` on {datetime.now().strftime('%Y-%m-%d %H:%M')} against "
        f"{psycopg.connect(DATABASE_URL).execute('SHOW server_version').fetchone()[0]}. Two real connections, A and B, "
        "run the commands below against a scratch database built from this project's migrations "
        "(account 4 = main wallet, account 5 = savings wallet, both owned by user 1). "
        "Lines starting with `--` are comments added by the script; everything else is the actual command and result.",
        "",
    ]
    for title, tr in sections:
        out += [f"## {title}", "", "```text", *tr, "```", "", answers[title], ""]
    out += [
        "## Which level does the transfer use?",
        "",
        "**Read Committed plus `SELECT ... FOR UPDATE`** on both account rows in ascending id order. The lock makes "
        "other transfers on those rows wait, and the balance is read *after* the lock is held, so the stale-read problem "
        "from Case 1 cannot hurt: the value checked is the value written against. Serializable would also be correct, "
        "but under contention on one hot wallet most transactions would abort with `40001` and need a retry (Case 3), "
        "which costs throughput for no extra safety here. Reconciliation is the exception: it runs at Repeatable Read "
        "because it needs one consistent snapshot across many tables (Case 4).",
        "",
    ]
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT}")
    print(facts)


if __name__ == "__main__":
    sys.exit(main())
