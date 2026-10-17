import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import config, db
from tests.helpers import computed_balance, funded_wallet, ledger_totals, stored_balance


@pytest.fixture
def api(live_url):
    with httpx.Client(base_url=live_url, timeout=30) as c:
        yield c


def user_h(uid):
    return {"X-User-Id": str(uid), "Idempotency-Key": str(uuid.uuid4())}


def admin_h():
    return {"X-Admin-Token": config.ADMIN_TOKEN, "Idempotency-Key": str(uuid.uuid4())}


def send(api, uid, src, dst, amount):
    r = api.post("/transfers", json={"from_wallet_id": src, "to_wallet_id": dst, "amount": amount}, headers=user_h(uid))
    assert r.status_code == 201, r.text
    return r.json()["transfer_id"]


def test_reversal_restores_balances_and_links_back(api):
    a, wa = funded_wallet(api, 10_000)
    b, wb = funded_wallet(api, 0)
    tid = send(api, a, wa, wb, 4_000)
    r = api.post(f"/transfers/{tid}/reverse", json={"note": "sent by mistake"}, headers=admin_h())
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["type"] == "reversal" and rev["reverses_transfer_id"] == tid
    with db.tx() as conn:
        assert stored_balance(conn, wa) == 10_000 and stored_balance(conn, wb) == 0
        assert computed_balance(conn, wa) == 10_000
        d, c = ledger_totals(conn)
        assert d == c
        # the original ledger rows are untouched; the reversal wrote two new ones
        assert conn.execute("SELECT count(*) AS n FROM ledger_entries WHERE transfer_id=%s", (tid,)).fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM ledger_entries WHERE transfer_id=%s",
                            (rev["transfer_id"],)).fetchone()["n"] == 2


def test_second_reversal_is_409_even_with_a_new_key(api):
    a, wa = funded_wallet(api, 10_000)
    b, wb = funded_wallet(api, 0)
    tid = send(api, a, wa, wb, 1_000)
    assert api.post(f"/transfers/{tid}/reverse", json={}, headers=admin_h()).status_code == 201
    r = api.post(f"/transfers/{tid}/reverse", json={}, headers=admin_h())
    assert r.status_code == 409 and r.json()["code"] == "REVERSAL_NOT_POSSIBLE"


def test_concurrent_reversals_make_exactly_one(live_url, api):
    a, wa = funded_wallet(api, 10_000)
    b, wb = funded_wallet(api, 0)
    tid = send(api, a, wa, wb, 1_000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rs = list(pool.map(lambda _: httpx.post(f"{live_url}/transfers/{tid}/reverse", json={},
                                                headers=admin_h(), timeout=30), range(8)))
    assert sorted(r.status_code for r in rs) == [201] + [409] * 7
    with db.tx() as conn:
        assert stored_balance(conn, wa) == 10_000


def test_cannot_reverse_a_reversal_or_missing_transfer(api):
    a, wa = funded_wallet(api, 10_000)
    b, wb = funded_wallet(api, 0)
    tid = send(api, a, wa, wb, 1_000)
    rev = api.post(f"/transfers/{tid}/reverse", json={}, headers=admin_h()).json()["transfer_id"]
    r = api.post(f"/transfers/{rev}/reverse", json={}, headers=admin_h())
    assert r.status_code == 409 and r.json()["code"] == "REVERSAL_NOT_POSSIBLE"
    assert api.post("/transfers/999999999/reverse", json={}, headers=admin_h()).status_code == 404


def test_reversal_fails_if_recipient_already_spent_the_money(api):
    a, wa = funded_wallet(api, 10_000)
    b, wb = funded_wallet(api, 0)
    c, wc = funded_wallet(api, 0)
    tid = send(api, a, wa, wb, 1_000)
    send(api, b, wb, wc, 1_000)
    r = api.post(f"/transfers/{tid}/reverse", json={}, headers=admin_h())
    assert r.status_code == 409 and r.json()["code"] == "REVERSAL_NOT_POSSIBLE"
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM transfers WHERE reverses_transfer_id=%s", (tid,)).fetchone()["n"]
        assert n == 0


def test_reverse_requires_admin(api):
    a, wa = funded_wallet(api, 10_000)
    b, wb = funded_wallet(api, 0)
    tid = send(api, a, wa, wb, 1_000)
    assert api.post(f"/transfers/{tid}/reverse", json={}, headers=user_h(a)).status_code == 401
    bad = {**admin_h(), "X-Admin-Token": "wrong"}
    assert api.post(f"/transfers/{tid}/reverse", json={}, headers=bad).status_code == 401


def test_get_transfer_shows_entries_history_and_enforces_access(api):
    a, wa = funded_wallet(api, 10_000)
    b, wb = funded_wallet(api, 0)
    outsider, _ = funded_wallet(api, 0)
    tid = send(api, a, wa, wb, 2_500)
    r = api.get(f"/transfers/{tid}", headers={"X-User-Id": str(b)})
    assert r.status_code == 200
    body = r.json()
    assert body["amount"] == 2_500 and body["status"] == "completed"
    assert sorted(e["direction"] for e in body["entries"]) == ["credit", "debit"]
    assert body["status_history"][0]["from_status"] is None
    assert body["status_history"][0]["to_status"] == "completed"
    assert body["status_history"][0]["changed_by"] == f"user:{a}"
    assert body["reversed_by_transfer_id"] is None
    assert api.get(f"/transfers/{tid}", headers={"X-User-Id": str(outsider)}).status_code == 403
    assert api.get(f"/transfers/{tid}").status_code == 401
    assert api.get("/transfers/999999999", headers={"X-User-Id": str(a)}).status_code == 404
    rev = api.post(f"/transfers/{tid}/reverse", json={}, headers=admin_h()).json()
    admin = {"X-Admin-Token": config.ADMIN_TOKEN}
    assert api.get(f"/transfers/{tid}", headers=admin).json()["reversed_by_transfer_id"] == rev["transfer_id"]
    rbody = api.get(f"/transfers/{rev['transfer_id']}", headers=admin).json()
    assert rbody["status_history"][0]["changed_by"] == "admin:1"
