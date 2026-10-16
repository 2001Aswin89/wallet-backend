"""Milestone 2 acceptance: Rs 500 in one wallet, 100 concurrent Rs 10 transfers out of it.
Exactly 50 succeed, the wallet ends at 0, never negative, debits == credits. Repeated 20 times."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import db
from tests.helpers import computed_balance, funded_wallet, ledger_totals, stored_balance

RUNS = 20


def _send(url, uid, src, dst, amount):
    r = httpx.post(f"{url}/transfers", json={"from_wallet_id": src, "to_wallet_id": dst, "amount": amount},
                   headers={"X-User-Id": str(uid), "Idempotency-Key": str(uuid.uuid4())}, timeout=60)
    return r.status_code, r.json()


@pytest.fixture
def api(live_url):
    with httpx.Client(base_url=live_url, timeout=30) as c:
        yield c


def test_stampede_100_concurrent_transfers_20_times(live_url, api):
    for run in range(RUNS):
        uid, src = funded_wallet(api, 50_000)       # Rs 500
        _, dst = funded_wallet(api, 0)
        with ThreadPoolExecutor(max_workers=100) as pool:
            results = list(pool.map(lambda _: _send(live_url, uid, src, dst, 1_000), range(100)))
        codes = [c for c, _ in results]
        ok = codes.count(201)
        short = [b["code"] for c, b in results if c == 422]
        assert ok == 50, f"run {run}: {ok} succeeded, codes={sorted(set(codes))}"
        assert short.count("INSUFFICIENT_FUNDS") == 50, f"run {run}: {short}"
        with db.tx() as conn:
            assert stored_balance(conn, src) == 0, f"run {run}"
            assert computed_balance(conn, src) == 0, f"run {run}"
            assert stored_balance(conn, dst) == 50_000
            debits, credits = ledger_totals(conn)
            assert debits == credits, f"run {run}"


def test_opposite_direction_transfers_do_not_deadlock(live_url, api):
    ua, wa = funded_wallet(api, 1_000_000)
    ub, wb = funded_wallet(api, 1_000_000)
    jobs = [(ua, wa, wb)] * 50 + [(ub, wb, wa)] * 50
    with ThreadPoolExecutor(max_workers=100) as pool:
        results = list(pool.map(lambda j: _send(live_url, j[0], j[1], j[2], 100), jobs))
    assert [c for c, _ in results].count(201) == 100, [r for r in results if r[0] != 201][:3]
    with db.tx() as conn:
        assert stored_balance(conn, wa) == 1_000_000 and stored_balance(conn, wb) == 1_000_000
        assert stored_balance(conn, wa) == computed_balance(conn, wa)
