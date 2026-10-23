"""Milestone 4 performance: EXPLAIN (ANALYZE, BUFFERS) on every reconciliation check.

    DATABASE_URL=...wallet_1m python -m scripts.explain_checks

For each check it runs three variants: as-is, without idx_ledger_transfer, and without
idx_ledger_account_time (DROP INDEX inside a transaction that is rolled back, so nothing is
lost). That shows which checks actually depend on an index and which are a full scan either way.
Writes docs/performance/reconciliation_explain.md.
"""
import re
from pathlib import Path

import psycopg

from app.config import DATABASE_URL
from app.worker import reconcile as r

OUT = Path(__file__).resolve().parent.parent / "docs" / "performance" / "reconciliation_explain.md"

CHECKS = [
    ("1. global_balance (sum of debits = sum of credits)", r.SQL_GLOBAL),
    ("2. transfer_balance (every transfer balances)", r.SQL_TRANSFER_BALANCE),
    ("3+4. stored_balance and overdraft (one pass over accounts + ledger)", r.SQL_ACCOUNT_BALANCES),
    ("5a. orphan_transfer (completed transfer with no entries)", r.SQL_COMPLETED_WITHOUT_ENTRIES),
    ("5b. orphan_entry (entry with no transfer)", r.SQL_ENTRIES_WITHOUT_TRANSFER),
    ("6. transfer_amount (entries add up to the transfer amount)", r.SQL_TRANSFER_AMOUNT),
]
VARIANTS = [("as-is", None), ("without idx_ledger_transfer", "idx_ledger_transfer"),
            ("without idx_ledger_account_time", "idx_ledger_account_time")]


def explain(conn, sql: str, drop: str | None) -> tuple[str, float]:
    with conn.transaction(force_rollback=True):               # the DROP INDEX never survives
        if drop:
            conn.execute(f"DROP INDEX {drop}")
        plan = "\n".join(row[0] for row in conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql))
    ms = float(re.search(r"Execution Time: ([\d.]+) ms", plan).group(1))
    return plan, ms


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(DATABASE_URL) as conn:
        conn.autocommit = True
        conn.execute("SET max_parallel_workers_per_gather = 2")
        sizes = conn.execute("SELECT (SELECT count(*) FROM ledger_entries), (SELECT count(*) FROM transfers), "
                             "(SELECT count(*) FROM accounts), version()").fetchone()
        conn.autocommit = False
        summary, details = [], []
        for title, sql in CHECKS:
            row = [title]
            details.append(f"## {title}\n")
            for label, drop in VARIANTS:
                conn.execute("SET max_parallel_workers_per_gather = 2")
                explain(conn, sql, drop)                                   # warm the cache
                plan, ms = explain(conn, sql, drop)
                row.append(f"{ms:,.0f} ms")
                details.append(f"### {label}: {ms:,.1f} ms\n\n```text\n{plan}\n```\n")
            summary.append(row)
    head = (f"# Reconciliation checks at scale\n\nDatabase: {sizes[0]:,} ledger entries, {sizes[1]:,} transfers, "
            f"{sizes[2]:,} accounts. {sizes[3]}.\nEach number is `EXPLAIN (ANALYZE, BUFFERS)` execution time on a "
            "warm cache (second run of two). Variants drop an index inside a rolled-back transaction.\n\n"
            "| check | as-is | without idx_ledger_transfer | without idx_ledger_account_time |\n|---|---|---|---|\n"
            + "\n".join("| " + " | ".join(r_) + " |" for r_ in summary) + "\n\n")
    OUT.write_text(head + "\n".join(details), encoding="utf-8")
    print(head)


if __name__ == "__main__":
    main()
