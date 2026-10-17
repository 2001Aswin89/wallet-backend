"""Apply migrations/*.sql in filename order, once each. Safe to run from api and worker at once."""
import logging
import time
from pathlib import Path

import psycopg

from app.config import DATABASE_URL

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
ADVISORY_LOCK_KEY = 72_001  # arbitrary constant shared by every process that migrates
log = logging.getLogger("wallet.migrate")


def wait_for_db(dsn: str | None = None, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with psycopg.connect(dsn or DATABASE_URL, connect_timeout=3):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def run_migrations(dsn: str | None = None) -> list[str]:
    wait_for_db(dsn)
    applied_now: list[str] = []
    with psycopg.connect(dsn or DATABASE_URL, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
                applied_now.append(path.name)
                log.info("applied migration %s", path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    return applied_now
