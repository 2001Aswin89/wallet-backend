"""Fraud rules (Milestone 5, Part A). Each rule is one SQL query run INSIDE the transfer transaction,
after the sender's row is locked, so two concurrent transfers from one wallet cannot both slip
under a limit. Thresholds live in the risk_rules table; nothing is hard-coded here.

    VELOCITY         hold   >= threshold_value outgoing transfers in window_seconds (this would be the 6th)
                            index: idx_transfers_from_time (from_account_id, created_at DESC)
    AMOUNT_SPIKE     hold   amount > threshold_value x the wallet's average completed outgoing transfer
                            over window_seconds (no history: does not fire)
                            index: idx_transfers_from_time
    NEW_PAYEE_LARGE  flag   first completed transfer to this recipient, amount > threshold_value paise
                            (only looked up when the amount is large)  index: idx_transfers_pair

Assumptions: rules apply to outgoing transfers from user wallets (peer transfers and withdrawals);
top-ups and admin reversals are skipped. Window queries count completed AND pending_review
transfers for VELOCITY (a held transfer still counts as activity), completed only for the others.
"""
from dataclasses import dataclass, field

from psycopg.types.json import Jsonb


@dataclass
class Hit:
    rule_id: int
    code: str
    action: str                      # 'hold' | 'flag'
    details: dict = field(default_factory=dict)


def load_rules(conn) -> dict[str, dict]:
    """Loaded once per transaction. Disabled rules are simply absent."""
    rows = conn.execute("SELECT id, code, threshold_value, window_seconds, action FROM risk_rules "
                        "WHERE enabled").fetchall()
    return {r["code"]: r for r in rows}


def evaluate_rules(conn, rules: dict[str, dict], *, kind: str, from_id: int, to_id: int, amount: int) -> list[Hit]:
    hits: list[Hit] = []

    r = rules.get("VELOCITY")
    if r:
        n = conn.execute(
            "SELECT count(*) AS n FROM transfers WHERE from_account_id = %s "
            "AND status IN ('completed', 'pending_review') AND created_at > now() - make_interval(secs => %s)",
            (from_id, r["window_seconds"]),
        ).fetchone()["n"]
        if n >= r["threshold_value"]:
            hits.append(Hit(r["id"], "VELOCITY", r["action"],
                            {"transfers_in_window": n, "limit": r["threshold_value"],
                             "window_seconds": r["window_seconds"]}))

    r = rules.get("AMOUNT_SPIKE")
    if r:
        avg = conn.execute(
            "SELECT avg(amount) AS avg FROM transfers WHERE from_account_id = %s AND status = 'completed' "
            "AND created_at > now() - make_interval(secs => %s)",
            (from_id, r["window_seconds"]),
        ).fetchone()["avg"]
        if avg is not None and amount > r["threshold_value"] * avg:
            hits.append(Hit(r["id"], "AMOUNT_SPIKE", r["action"],
                            {"amount": amount, "average": round(float(avg), 2),
                             "multiplier_limit": r["threshold_value"]}))

    r = rules.get("NEW_PAYEE_LARGE")
    if r and kind == "peer_transfer" and amount > r["threshold_value"]:
        seen = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM transfers WHERE from_account_id = %s AND to_account_id = %s "
            "AND status = 'completed') AS seen", (from_id, to_id),
        ).fetchone()["seen"]
        if not seen:
            hits.append(Hit(r["id"], "NEW_PAYEE_LARGE", r["action"],
                            {"amount": amount, "threshold": r["threshold_value"], "first_transfer_to_payee": True}))
    return hits


def record_flags(conn, transfer_id: int, hits: list[Hit]) -> None:
    """One risk_flags row per rule that fired, each with its own action."""
    for h in hits:
        conn.execute(
            "INSERT INTO risk_flags (transfer_id, rule_id, action_taken, details) VALUES (%s, %s, %s, %s)",
            (transfer_id, h.rule_id, "held" if h.action == "hold" else "flagged", Jsonb(h.details)),
        )
