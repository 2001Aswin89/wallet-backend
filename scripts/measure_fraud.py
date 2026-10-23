"""Milestone 5, Part A: how much time do the fraud rules add to a transfer?

    DATABASE_URL=...wallet_1m python -m scripts.measure_fraud

(1) Each rule's query on its own, 1,000 samples against wallets with ~500 transfers of history.
(2) End-to-end transfer latency over 1,000 real transfers with the rules disabled vs enabled,
    alternating off/on/off/on so drift (cache, autovacuum) hits both equally. Thresholds are
    raised so no transfer is held: this measures the cost of CHECKING, not of the hold path.
Writes docs/performance/fraud_overhead.md. Thresholds are restored afterwards.
"""
import random
import statistics
import time
from pathlib import Path

from app import db
from app.services import fraud
from app.services.transfer import execute_transfer

OUT = Path(__file__).resolve().parent.parent / "docs" / "performance" / "fraud_overhead.md"
N = 1_000
ROUNDS = 2


def pct(samples, p):
    s = sorted(samples)
    return s[min(len(s) - 1, int(len(s) * p))]


def stats_row(label, ms):
    return f"| {label} | {statistics.mean(ms):.3f} | {statistics.median(ms):.3f} | {pct(ms, 0.95):.3f} | {len(ms)} |"


def main() -> None:
    db.open_pool()
    rng = random.Random(7)
    with db.tx() as conn:
        wallets = conn.execute(
            "SELECT id, user_id, balance FROM accounts WHERE account_type = 'user_wallet' AND balance >= 200000").fetchall()
        n_acct, n_tr = (conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"],
                        conn.execute("SELECT count(*) AS n FROM transfers").fetchone()["n"])
        orig = conn.execute("SELECT id, enabled, threshold_value, window_seconds FROM risk_rules").fetchall()
        rules = fraud.load_rules(conn)
    jobs = []
    for _ in range(N):
        src, dst = rng.sample(wallets, 2)
        jobs.append((src, dst["id"], rng.randint(100, 1_000)))

    # (1) each rule's query alone ------------------------------------------------------------
    per_rule = {}
    for code in ("VELOCITY", "AMOUNT_SPIKE", "NEW_PAYEE_LARGE"):
        ms = []
        rule = {code: dict(rules[code], threshold_value=(10**9 if code != "NEW_PAYEE_LARGE" else 0))}
        with db.tx() as conn:
            for src, dst_id, amount in jobs:
                t = time.perf_counter()
                fraud.evaluate_rules(conn, rule, kind="peer_transfer", from_id=src["id"], to_id=dst_id,
                                     amount=amount if code != "NEW_PAYEE_LARGE" else 1)
                ms.append((time.perf_counter() - t) * 1000)
        per_rule[code] = ms

    # (2) end to end ---------------------------------------------------------------------------
    def set_rules(enabled: bool):
        with db.tx() as conn:
            conn.execute("UPDATE risk_rules SET enabled = %s", (enabled,))
            conn.execute("UPDATE risk_rules SET threshold_value = 1000000000 WHERE code IN ('VELOCITY','AMOUNT_SPIKE')")
            conn.execute("UPDATE risk_rules SET threshold_value = 1000000000000 WHERE code = 'NEW_PAYEE_LARGE'")

    results = {False: [], True: []}
    try:
        for _ in range(ROUNDS):
            for enabled in (False, True):
                set_rules(enabled)
                for src, dst_id, amount in jobs:
                    t = time.perf_counter()
                    with db.tx() as conn:
                        execute_transfer(conn, kind="peer_transfer", caller_id=src["user_id"], wallet_id=src["id"],
                                         other_wallet_id=dst_id, amount=amount)
                    results[enabled].append((time.perf_counter() - t) * 1000)
    finally:
        with db.tx() as conn:
            for r in orig:
                conn.execute("UPDATE risk_rules SET enabled=%s, threshold_value=%s, window_seconds=%s WHERE id=%s",
                             (r["enabled"], r["threshold_value"], r["window_seconds"], r["id"]))
    off, on = results[False], results[True]
    added = statistics.mean(on) - statistics.mean(off)

    out = [
        "# Fraud-rule overhead", "",
        f"Database: {n_tr:,} transfers across {n_acct:,} accounts (about {n_tr * 2 // n_acct} ledger entries per wallet). "
        f"{N:,} transfers per mode per round, {ROUNDS} rounds, alternating off/on. Times in milliseconds, "
        "measured with `time.perf_counter()` in the application process (one pooled connection, localhost).", "",
        "## Each rule's query on its own", "",
        "| rule | mean | p50 | p95 | samples |", "|---|---|---|---|---|",
        stats_row("VELOCITY (count in window, `idx_transfers_from_time`)", per_rule["VELOCITY"]),
        stats_row("AMOUNT_SPIKE (avg over 30 days, `idx_transfers_from_time`)", per_rule["AMOUNT_SPIKE"]),
        stats_row("NEW_PAYEE_LARGE (EXISTS, `idx_transfers_pair`; large amount forced)", per_rule["NEW_PAYEE_LARGE"]), "",
        "## End-to-end peer transfer (BEGIN to COMMIT)", "",
        "| rules | mean | p50 | p95 | samples |", "|---|---|---|---|---|",
        stats_row("disabled", off), stats_row("enabled (none fire)", on), "",
        f"**Added by the rules: {added:.3f} ms on average per transfer "
        f"({added / statistics.mean(off) * 100:.1f}% of the {statistics.mean(off):.3f} ms baseline), "
        f"{statistics.median(on) - statistics.median(off):.3f} ms at the median.**", "",
        "NEW_PAYEE_LARGE only runs its query when the amount exceeds the threshold, so on ordinary transfers it costs "
        "one comparison; the figure above is the worst case. VELOCITY and AMOUNT_SPIKE run on every outgoing transfer.", "",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    db.close_pool()


if __name__ == "__main__":
    main()
