import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _db_pool():
    """Tests that use app.db directly (assertions on the database) need an open pool even
    when no TestClient is running."""
    from app import db
    from app.migrate import run_migrations

    run_migrations()
    own = db.pool is None
    if own:
        db.open_pool()
    yield
    if own:
        db.close_pool()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:  # runs lifespan: migrations + pool
        yield c


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_url():
    """A real uvicorn process, for tests that need true concurrency (stampede, idempotency)."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        env=os.environ.copy(),
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("uvicorn did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)
