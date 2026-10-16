"""Milestone 3 acceptance tests, against a real uvicorn process and real Postgres."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import db
from tests.helpers import funded_wallet, stored_balance


@pytest.fixture
def api(live_url):
    with httpx.Client(base_url=live_url, timeout=30) as c:
        yield c


def _hdr(uid, key):
    return {"X-User-Id": str(uid), "Idempotency-Key": key}


def _count(conn, src):
    return conn.execute("SELECT count(*) AS n FROM transfers WHERE from_account_id=%s", (src,)).fetchone()["n"]


def test_same_request_5_times_creates_one_transfer_and_identical_responses(api):
    a, wa = funded_wallet(api, 100_000)
    _, wb = funded_wallet(api, 0)
    key, body = str(uuid.uuid4()), {"from_wallet_id": wa, "to_wallet_id": wb, "amount": 2_500, "note": "rent"}
    rs = [api.post("/transfers", json=body, headers=_hdr(a, key)) for _ in range(5)]
    assert [r.status_code for r in rs] == [201] * 5
    assert len({r.content for r in rs}) == 1                      # byte-identical
    assert len({r.json()["transfer_id"] for r in rs}) == 1
    with db.tx() as conn:
        assert _count(conn, wa) == 1
        assert stored_balance(conn, wa) == 97_500


def test_same_key_10_in_parallel_creates_exactly_one_transfer(live_url, api):
    a, wa = funded_wallet(api, 100_000)
    _, wb = funded_wallet(api, 0)
    key, body = str(uuid.uuid4()), {"from_wallet_id": wa, "to_wallet_id": wb, "amount": 1_000}

    def go(_):
        return httpx.post(f"{live_url}/transfers", json=body, headers=_hdr(a, key), timeout=60)

    for _ in range(5):  # repeat to shake out timing luck
        key = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=10) as pool:
            rs = list(pool.map(go, range(10)))
        assert [r.status_code for r in rs] == [201] * 10, [(r.status_code, r.text) for r in rs]
        assert len({r.content for r in rs}) == 1
    with db.tx() as conn:
        assert _count(conn, wa) == 5                               # one per round, not 50
        assert stored_balance(conn, wa) == 95_000


def test_same_key_different_amount_is_422_and_no_money_moves(api):
    a, wa = funded_wallet(api, 100_000)
    _, wb = funded_wallet(api, 0)
    key = str(uuid.uuid4())
    first = api.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": 1_000}, headers=_hdr(a, key))
    assert first.status_code == 201
    second = api.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": 9_000}, headers=_hdr(a, key))
    assert second.status_code == 422 and second.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert set(second.json()) == {"code", "message", "request_id"}
    with db.tx() as conn:
        assert _count(conn, wa) == 1
        assert stored_balance(conn, wa) == 99_000


def test_missing_or_invalid_key_is_400_on_every_post(api):
    a, wa = funded_wallet(api, 1_000)
    h = {"X-User-Id": str(a)}
    posts = [
        ("/users", {"name": "N", "email": "n@example.com", "phone": "+919876543210"}),
        (f"/users/{a}/wallets", {"label": "x"}),
        ("/transfers/topup", {"wallet_id": wa, "amount": 10}),
        ("/transfers", {"from_wallet_id": wa, "to_wallet_id": wa + 1, "amount": 10}),
        ("/transfers/withdraw", {"wallet_id": wa, "amount": 10}),
    ]
    for path, body in posts:
        r = api.post(path, json=body, headers=h)
        assert r.status_code == 400 and r.json()["code"] == "MISSING_IDEMPOTENCY_KEY", path
        r = api.post(path, json=body, headers={**h, "Idempotency-Key": "not-a-uuid"})
        assert r.status_code == 400 and r.json()["code"] == "INVALID_IDEMPOTENCY_KEY", path


def test_key_is_scoped_per_user(api):
    a, wa = funded_wallet(api, 10_000)
    b, wb = funded_wallet(api, 10_000)
    key = str(uuid.uuid4())
    ra = api.post("/transfers/withdraw", json={"wallet_id": wa, "amount": 100}, headers=_hdr(a, key))
    rb = api.post("/transfers/withdraw", json={"wallet_id": wb, "amount": 100}, headers=_hdr(b, key))
    assert ra.status_code == rb.status_code == 201
    assert ra.json()["transfer_id"] != rb.json()["transfer_id"]


def test_failed_request_is_re_evaluated_not_replayed(api):
    """Documented assumption: a failure rolls the key back, so the same key can succeed later."""
    a, wa = funded_wallet(api, 500)
    _, wb = funded_wallet(api, 0)
    key, body = str(uuid.uuid4()), {"from_wallet_id": wa, "to_wallet_id": wb, "amount": 800}
    assert api.post("/transfers", json=body, headers=_hdr(a, key)).json()["code"] == "INSUFFICIENT_FUNDS"
    assert api.post("/transfers/topup", json={"wallet_id": wa, "amount": 1_000},
                    headers=_hdr(a, str(uuid.uuid4()))).status_code == 201
    r = api.post("/transfers", json=body, headers=_hdr(a, key))
    assert r.status_code == 201


def test_create_user_is_idempotent_without_a_user(api):
    key = str(uuid.uuid4())
    body = {"name": "Once", "email": f"once{uuid.uuid4().hex}@example.com", "phone": f"+91{uuid.uuid4().int % 10**10:010d}"}
    r1 = api.post("/users", json=body, headers={"Idempotency-Key": key})
    r2 = api.post("/users", json=body, headers={"Idempotency-Key": key})
    assert r1.status_code == r2.status_code == 201
    assert r1.content == r2.content
    r3 = api.post("/users", json={**body, "name": "Other"}, headers={"Idempotency-Key": key})
    assert r3.status_code == 422 and r3.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
