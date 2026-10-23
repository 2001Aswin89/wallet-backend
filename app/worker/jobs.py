"""The nightly pipeline: reconciliation, flag explanations, then housekeeping."""
import logging

import psycopg

from app.config import DATABASE_URL
from app.worker.explain_flags import explain_flags
from app.worker.reconcile import run_reconciliation

log = logging.getLogger("wallet.jobs")


def cleanup_idempotency_keys(dsn: str | None = None) -> int:
    """Optional Milestone 3 item: keys older than 24 hours are no longer needed for replays."""
    with psycopg.connect(dsn or DATABASE_URL) as conn:
        n = conn.execute("DELETE FROM idempotency_keys WHERE created_at < now() - interval '24 hours'").rowcount
    log.info("deleted %s idempotency keys older than 24h", n)
    return n


def nightly() -> None:
    result = run_reconciliation(triggered_by="schedule")
    log.info("nightly reconciliation: %s", result)
    try:                                  # an LLM outage must not stop the housekeeping below
        from app.llm import get_llm
        log.info("nightly flag explanations: %s", explain_flags(get_llm()))
    except Exception:
        log.exception("flag explanation step failed")
    cleanup_idempotency_keys()
