"""Nightly reconciliation (Milestone 4). Reports problems; NEVER fixes them.

Concurrency: the whole job runs in ONE REPEATABLE READ transaction, so every check sees the same
snapshot even while transfers keep committing. It starts with SELECT ... FOR UPDATE NOWAIT on the
job_locks row: a second run fails to get the lock and is recorded as 'skipped'. The report rows are
written inside that same transaction, so the lock covers writing the report. If the job crashes,
the transaction rolls back and the run row is closed as 'error' from a fresh transaction.

Root causes, not raw rows: one physical corruption trips several checks (an altered entry
unbalances its transfer, the global sum and its account). Every issue row therefore carries a
group_key naming its root cause, and issue_count is the number of distinct group_keys.
"""
import logging

import psycopg
from psycopg import IsolationLevel
from psycopg.errors import LockNotAvailable
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

log = logging.getLogger("wallet.reconcile")
JOB_NAME = "reconciliation"
CHECKS_RUN = 7

# --- the checks (also timed with EXPLAIN in scripts/explain_checks.sql) -----------------------
SQL_GLOBAL = """
SELECT COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)::bigint  AS debits,
       COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)::bigint AS credits
FROM ledger_entries"""

SQL_TRANSFER_BALANCE = """
SELECT transfer_id,
       COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)::bigint  AS debits,
       COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)::bigint AS credits
FROM ledger_entries
GROUP BY transfer_id
HAVING COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)
    <> COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)"""

# Entries must also add up to the transfer's own amount (completed transfers that have entries).
SQL_TRANSFER_AMOUNT = """
SELECT t.id AS transfer_id, t.amount, e.debits, e.credits
FROM transfers t
JOIN (SELECT transfer_id,
             COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)::bigint  AS debits,
             COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)::bigint AS credits
      FROM ledger_entries GROUP BY transfer_id) e ON e.transfer_id = t.id
WHERE t.status = 'completed' AND (e.debits <> t.amount OR e.credits <> t.amount)"""

# One pass over the ledger feeds both "stored = computed" and "no overdraft".
SQL_ACCOUNT_BALANCES = """
SELECT a.id, a.account_type, a.balance AS stored,
       COALESCE(SUM(CASE le.direction WHEN 'credit' THEN le.amount ELSE -le.amount END), 0)::bigint AS computed
FROM accounts a LEFT JOIN ledger_entries le ON le.account_id = a.id
GROUP BY a.id, a.account_type, a.balance"""

SQL_COMPLETED_WITHOUT_ENTRIES = """
SELECT t.id FROM transfers t
WHERE t.status = 'completed'
  AND NOT EXISTS (SELECT 1 FROM ledger_entries le WHERE le.transfer_id = t.id)"""

SQL_ENTRIES_WITHOUT_TRANSFER = """
SELECT le.id, le.transfer_id FROM ledger_entries le
LEFT JOIN transfers t ON t.id = le.transfer_id WHERE t.id IS NULL"""


def _issue(check, subject_type, subject_id, expected, actual, group_key, **detail):
    return {"check_name": check, "subject_type": subject_type, "subject_id": subject_id,
            "expected": expected, "actual": actual, "group_key": group_key, "detail": detail}


def _deltas_for(conn, transfer_ids: list[int]) -> dict[int, dict[int, int]]:
    """For each flagged transfer: per-account (actual signed entries - what the transfer row says
    they should be). credit is +, debit is -. Only completed transfers are expected to have entries."""
    if not transfer_ids:
        return {}
    expected: dict[int, dict[int, int]] = {}
    for t in conn.execute("SELECT id, status, amount, from_account_id, to_account_id FROM transfers "
                          "WHERE id = ANY(%s)", (transfer_ids,)):
        if t["status"] == "completed":
            expected[t["id"]] = {t["from_account_id"]: -t["amount"], t["to_account_id"]: t["amount"]}
    deltas: dict[int, dict[int, int]] = {tid: {} for tid in transfer_ids}
    for tid, per_acct in expected.items():
        for acct, v in per_acct.items():
            deltas[tid][acct] = -v
    for e in conn.execute("SELECT transfer_id, account_id, direction, amount FROM ledger_entries "
                          "WHERE transfer_id = ANY(%s)", (transfer_ids,)):
        signed = e["amount"] if e["direction"] == "credit" else -e["amount"]
        deltas[e["transfer_id"]][e["account_id"]] = deltas[e["transfer_id"]].get(e["account_id"], 0) + signed
    return deltas


def _explaining_group(acct_id: int, deltas, fits) -> str:
    """Name the flagged transfer whose damage explains this account's anomaly, else the account itself.
    fits(delta_sum) -> True when removing that damage makes the account look healthy."""
    touching = [t for t in sorted(deltas) if deltas[t].get(acct_id, 0) != 0]
    for t in touching:
        if fits(deltas[t][acct_id]):
            return f"transfer:{t}"
    if len(touching) > 1 and fits(sum(deltas[t][acct_id] for t in touching)):
        return f"transfer:{touching[0]}"
    return f"account:{acct_id}"


def collect_issues(conn) -> list[dict]:
    issues: list[dict] = []

    # 2. every transfer balances        6. entries add up to the transfer amount
    unbalanced = conn.execute(SQL_TRANSFER_BALANCE).fetchall()
    wrong_amount = conn.execute(SQL_TRANSFER_AMOUNT).fetchall()
    # 5a / 5b. nothing orphaned
    no_entries = conn.execute(SQL_COMPLETED_WITHOUT_ENTRIES).fetchall()
    orphan_entries = conn.execute(SQL_ENTRIES_WITHOUT_TRANSFER).fetchall()
    # 3 + 4. per-account stored vs computed balance, overdrafts
    accounts = conn.execute(SQL_ACCOUNT_BALANCES).fetchall()
    # 1. everything balances
    g = conn.execute(SQL_GLOBAL).fetchone()

    flagged = sorted({r["transfer_id"] for r in unbalanced} | {r["transfer_id"] for r in wrong_amount}
                     | {r["id"] for r in no_entries})
    deltas = _deltas_for(conn, flagged)

    for r in unbalanced:
        issues.append(_issue("transfer_balance", "transfer", r["transfer_id"], r["debits"], r["credits"],
                             f"transfer:{r['transfer_id']}", note="expected debits == credits"))
    for r in wrong_amount:
        bad = r["debits"] if r["debits"] != r["amount"] else r["credits"]
        issues.append(_issue("transfer_amount", "transfer", r["transfer_id"], r["amount"], bad,
                             f"transfer:{r['transfer_id']}", debits=r["debits"], credits=r["credits"]))
    for r in no_entries:
        issues.append(_issue("orphan_transfer", "transfer", r["id"], None, 0, f"transfer:{r['id']}",
                             note="completed transfer has no ledger entries"))
    for r in orphan_entries:
        issues.append(_issue("orphan_entry", "entry", r["id"], None, None, f"entry:{r['id']}",
                             transfer_id=r["transfer_id"]))

    for a in accounts:
        mismatch = a["stored"] - a["computed"]
        if mismatch != 0:
            # computed = truth + delta, so a stored balance that the app wrote correctly differs by -delta
            group = _explaining_group(a["id"], deltas, lambda d, m=mismatch: m == -d)
            issues.append(_issue("stored_balance", "account", a["id"], a["computed"], a["stored"], group))
        if a["account_type"] == "user_wallet" and a["computed"] < 0:
            group = _explaining_group(a["id"], deltas, lambda d, c=a["computed"]: c - d >= 0)
            issues.append(_issue("overdraft", "account", a["id"], 0, a["computed"], group))

    diff = g["debits"] - g["credits"]
    if diff != 0:
        explained = sum(r["debits"] - r["credits"] for r in unbalanced)
        group = f"transfer:{min(r['transfer_id'] for r in unbalanced)}" if unbalanced and explained == diff \
            else "ledger:global"
        issues.append(_issue("global_balance", "ledger", None, g["debits"], g["credits"], group,
                             derived=group != "ledger:global"))
    return issues


# --- run bookkeeping ---------------------------------------------------------------------------
def create_run(triggered_by: str, dsn: str | None = None) -> int:
    with psycopg.connect(dsn or DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute("INSERT INTO reconciliation_runs (status, triggered_by) VALUES ('running', %s) "
                            "RETURNING id", (triggered_by,)).fetchone()["id"]


def _close_run(dsn, run_id, status, note=None):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE reconciliation_runs SET status = %s, finished_at = now(), note = %s WHERE id = %s",
                     (status, note, run_id))


def run_reconciliation(triggered_by: str = "schedule", run_id: int | None = None,
                       dsn: str | None = None) -> dict:
    dsn = dsn or DATABASE_URL
    if run_id is None:
        run_id = create_run(triggered_by, dsn)
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            with conn.transaction():                                      # BEGIN ... REPEATABLE READ
                try:
                    conn.execute("SELECT 1 FROM job_locks WHERE job_name = %s FOR UPDATE NOWAIT", (JOB_NAME,))
                except LockNotAvailable:
                    raise _Skipped()  # leaving the block rolls the aborted transaction back
                # The snapshot was taken by the statement above; all checks below share it.
                issues = collect_issues(conn)
                groups = {i["group_key"] for i in issues}
                status = "failed" if groups else "passed"
                with conn.cursor() as cur:
                    for i in issues:
                        cur.execute(
                            "INSERT INTO reconciliation_issues (run_id, check_name, subject_type, subject_id, "
                            "expected, actual, group_key, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (run_id, i["check_name"], i["subject_type"], i["subject_id"], i["expected"],
                             i["actual"], i["group_key"], Jsonb(i["detail"])))
                conn.execute(
                    "UPDATE reconciliation_runs SET status = %s, finished_at = now(), checks_run = %s, "
                    "issue_count = %s WHERE id = %s", (status, CHECKS_RUN, len(groups), run_id))
        # COMMIT: report rows and the job_locks row lock are released together.
        log.info("reconciliation run %s: %s, %s root cause(s)", run_id, status, len(groups))
        return {"run_id": run_id, "status": status, "issue_count": len(groups), "raw_issue_rows": len(issues)}
    except _Skipped:
        _close_run(dsn, run_id, "skipped", "skipped: another run holds the lock")
        log.info("reconciliation run %s skipped: another run holds the lock", run_id)
        return {"run_id": run_id, "status": "skipped", "issue_count": 0, "raw_issue_rows": 0}
    except Exception as e:  # the transaction rolled back; keep the failure visible
        log.exception("reconciliation run %s crashed", run_id)
        _close_run(dsn, run_id, "error", f"{type(e).__name__}: {e}"[:255])
        return {"run_id": run_id, "status": "error", "issue_count": 0, "raw_issue_rows": 0}


class _Skipped(Exception):
    pass
