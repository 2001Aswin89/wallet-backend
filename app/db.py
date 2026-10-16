from contextlib import contextmanager

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from app.config import DATABASE_URL

pool: ConnectionPool | None = None


def open_pool() -> None:
    global pool
    pool = ConnectionPool(
        DATABASE_URL, min_size=2, max_size=20, kwargs={"row_factory": dict_row}, open=False
    )
    pool.open(wait=True, timeout=30)


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None


@contextmanager
def tx():
    """One explicit transaction: COMMIT on clean exit, ROLLBACK on any exception.
    Default isolation (READ COMMITTED). Callers write plain SQL on the yielded connection."""
    assert pool is not None, "pool not open"
    with pool.connection() as conn:  # commits on exit, rolls back on exception
        yield conn
