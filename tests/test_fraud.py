"""Milestone 5, Part A: fraud rules inside the transfer transaction, the held-transfer flow,
admin queue and decision. Real server, real Postgres, thresholds changed through the risk_rules table."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import config, db
from tests.helpers import computed_balance, funded_wallet, ledger_totals, rules_state, stored_balance


@pytest.fixture
def api(live_url):
    with httpx.Client(base_url=live_url, timeout=30) as c:
        yield c


@pytest.fixture(autouse=True)
def default_rules():
    with rules_state(enabled=True):
        yield


def uh(uid):
    return {"X-User-Id": str(uid), "Idempotency-Key": str(uuid.uuid4())}


def ah():
    return {"X-Admin-Token": config.ADMIN_TOKEN, "Idempotency-Key": str(uuid.uuid4())}


def send(api, uid, src, dst, amount, **kw):
    return api.post("/transfers", json={"from_wallet_id": src, "to_wallet_id": dst, "amount": amount, **kw},
                    headers=uh(uid))


def entries(transfer_id):
    with db.tx() as conn:
        return conn.execute("SELECT count(*) AS n FROM ledger_entries WHERE transfer_id = %s",
                            (transfer_id,)).fetchone()["n"]


def flags(transfer_id):
    with db.tx() as conn:
        return conn.execute("SELECT r.code, f.action_taken, f.decision, f.details FROM risk_flags f "
                            "JOIN risk_rules r ON r.id = f.rule_id WHERE f.transfer_id = %s ORDER BY f.id",
                            (transfer_id,)).fetchall()


def test_velocity_holds_the_sixth_transfer_and_moves_no_money(api):
    a, wa = funded_wallet(api, 1_000_000)
    _, wb = funded_wallet(api, 0)
    for _ in range(5):
        assert send(api, a, wa, wb, 1_000).status_code == 201
    r = send(api, a, wa, wb, 1_000)
    assert r.status_code == 202, r.text
    held = r.json()
    assert held["status"] == "pending_review" and held["flags"] == ["VELOCITY"]
    assert entries(held["transfer_id"]) == 0
    with db.tx() as conn:
        assert stored_balance(conn, wa) == 1_000_000 - 5_000          # the held one did not move money
        assert stored_balance(conn, wb) == 5_000
    f = flags(held["transfer_id"])
    assert [(x["code"], x["action_taken"]) for x in f] == [("VELOCITY", "held")]
    assert f[0]["details"]["transfers_in_window"] == 5 and f[0]["details"]["limit"] == 5


def test_release_writes_entries_and_audits_the_admin(api):
    a, wa = funded_wallet(api, 1_000_000)
    _, wb = funded_wallet(api, 0)
    for _ in range(5):
        send(api, a, wa, wb, 1_000)
    tid = send(api, a, wa, wb, 2_000).json()["transfer_id"]

    q = api.get("/admin/risk/queue", headers={"X-Admin-Token": config.ADMIN_TOKEN}).json()
    item = next(i for i in q["items"] if i["transfer_id"] == tid)
    assert item["status"] == "pending_review" and item["flags"][0]["rule"] == "VELOCITY"

    r = api.post(f"/admin/risk/{tid}/decision", json={"decision": "release"}, headers=ah())
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert entries(tid) == 2
    with db.tx() as conn:
        assert stored_balance(conn, wa) == 1_000_000 - 7_000 == computed_balance(conn, wa)
        d, c = ledger_totals(conn)
        assert d == c
    hist = api.get(f"/transfers/{tid}", headers={"X-Admin-Token": config.ADMIN_TOKEN}).json()["status_history"]
    assert [(h["from_status"], h["to_status"], h["changed_by"]) for h in hist] == [
        (None, "pending_review", f"user:{a}"), ("pending_review", "completed", "admin:1")]
    assert flags(tid)[0]["decision"] == "release"
    # no longer held: a second decision is a 409
    again = api.post(f"/admin/risk/{tid}/decision", json={"decision": "release"}, headers=ah())
    assert again.status_code == 409 and again.json()["code"] == "TRANSFER_NOT_HELD"


def test_reverse_decision_rejects_without_touching_the_ledger(api):
    a, wa = funded_wallet(api, 1_000_000)
    _, wb = funded_wallet(api, 0)
    for _ in range(5):
        send(api, a, wa, wb, 1_000)
    tid = send(api, a, wa, wb, 2_000).json()["transfer_id"]
    r = api.post(f"/admin/risk/{tid}/decision", json={"decision": "reverse"}, headers=ah())
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert entries(tid) == 0
    with db.tx() as conn:
        assert stored_balance(conn, wa) == 995_000
    assert flags(tid)[0]["decision"] == "reverse"
    # a rejected transfer cannot be reversed either: nothing moved
    assert api.post(f"/transfers/{tid}/reverse", json={}, headers=ah()).status_code == 409


def test_release_rechecks_the_balance_and_stays_held_when_short(api):
    a, wa = funded_wallet(api, 250_000)
    _, wb = funded_wallet(api, 0)
    for _ in range(3):
        assert send(api, a, wa, wb, 20_000).status_code == 201    # average 20,000; 190,000 left
    held = send(api, a, wa, wb, 110_000)                          # > 5x average -> AMOUNT_SPIKE hold
    assert held.status_code == 202 and held.json()["flags"] == ["AMOUNT_SPIKE"]
    tid = held.json()["transfer_id"]
    # the wallet is drained while the transfer waits
    assert api.post("/transfers/withdraw", json={"wallet_id": wa, "amount": 90_000},
                    headers=uh(a)).status_code == 201
    r = api.post(f"/admin/risk/{tid}/decision", json={"decision": "release"}, headers=ah())
    assert r.status_code == 422 and r.json()["code"] == "INSUFFICIENT_FUNDS"
    with db.tx() as conn:
        status = conn.execute("SELECT status FROM transfers WHERE id=%s", (tid,)).fetchone()["status"]
    assert status == "pending_review" and entries(tid) == 0
    assert api.post(f"/admin/risk/{tid}/decision", json={"decision": "reverse"}, headers=ah()).status_code == 200


def test_amount_spike_needs_history_and_uses_the_multiplier(api):
    a, wa = funded_wallet(api, 1_000_000)
    _, wb = funded_wallet(api, 0)
    assert send(api, a, wa, wb, 500_000).status_code == 201          # no history: no average, no spike
    a2, wa2 = funded_wallet(api, 1_000_000)
    for _ in range(3):
        send(api, a2, wa2, wb, 1_000)
    assert send(api, a2, wa2, wb, 4_000).status_code == 201          # 4x the average: fine
    r = send(api, a2, wa2, wb, 20_000)                               # over 5x the average
    assert r.status_code == 202 and r.json()["flags"] == ["AMOUNT_SPIKE"]
    assert flags(r.json()["transfer_id"])[0]["details"]["multiplier_limit"] == 5


def test_new_payee_large_is_flagged_but_allowed_once(api):
    a, wa = funded_wallet(api, 5_000_000)
    _, wb = funded_wallet(api, 0)
    r = send(api, a, wa, wb, 1_000_001)                              # above Rs 10,000, first time
    assert r.status_code == 201 and r.json()["flags"] == ["NEW_PAYEE_LARGE"]
    assert entries(r.json()["transfer_id"]) == 2                     # allowed: money moved
    assert [(x["code"], x["action_taken"]) for x in flags(r.json()["transfer_id"])] == [("NEW_PAYEE_LARGE", "flagged")]
    q = api.get("/admin/risk/queue", headers={"X-Admin-Token": config.ADMIN_TOKEN}).json()
    assert r.json()["transfer_id"] in [i["transfer_id"] for i in q["items"]]
    again = send(api, a, wa, wb, 1_000_001)                          # payee is no longer new
    assert again.status_code == 201 and again.json()["flags"] == []
    small = send(api, a, wa, funded_wallet(api, 0)[1], 1_000_000)    # exactly Rs 10,000 is not "above"
    assert small.status_code == 201 and small.json()["flags"] == []


def test_withdrawals_are_checked_and_topups_are_not(api):
    a, wa = funded_wallet(api, 1_000_000)
    for _ in range(5):
        assert api.post("/transfers/withdraw", json={"wallet_id": wa, "amount": 100}, headers=uh(a)).status_code == 201
    r = api.post("/transfers/withdraw", json={"wallet_id": wa, "amount": 100}, headers=uh(a))
    assert r.status_code == 202 and r.json()["flags"] == ["VELOCITY"]
    for _ in range(8):                                               # top-ups never held
        assert api.post("/transfers/topup", json={"wallet_id": wa, "amount": 100}, headers=uh(a)).status_code == 201


def test_thresholds_come_from_the_database(api):
    a, wa = funded_wallet(api, 1_000_000)
    _, wb = funded_wallet(api, 0)
    with rules_state(VELOCITY={"threshold_value": 2}):
        assert send(api, a, wa, wb, 100).status_code == 201
        assert send(api, a, wa, wb, 100).status_code == 201
        assert send(api, a, wa, wb, 100).status_code == 202
    with rules_state(enabled=False):
        assert send(api, a, wa, wb, 100).status_code == 201          # disabled rule does not fire


def test_insufficient_funds_beats_a_hold(api):
    a, wa = funded_wallet(api, 10_000)
    _, wb = funded_wallet(api, 0)
    for _ in range(5):
        send(api, a, wa, wb, 100)
    r = send(api, a, wa, wb, 50_000)                                 # would be held, but cannot be funded
    assert r.status_code == 422 and r.json()["code"] == "INSUFFICIENT_FUNDS"


def test_concurrent_transfers_cannot_slip_under_the_velocity_limit(live_url, api):
    a, wa = funded_wallet(api, 1_000_000)
    _, wb = funded_wallet(api, 0)

    def go(_):
        return httpx.post(f"{live_url}/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": 100},
                          headers=uh(a), timeout=60).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(go, range(16)))
    assert sorted(codes) == [201] * 5 + [202] * 11, codes            # the sender row lock serialises the count
    with db.tx() as conn:
        assert stored_balance(conn, wa) == 1_000_000 - 500


def test_decision_endpoint_needs_admin_and_a_key(api):
    a, wa = funded_wallet(api, 1_000_000)
    assert api.post("/admin/risk/1/decision", json={"decision": "release"},
                    headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 401
    assert api.post("/admin/risk/1/decision", json={"decision": "release"},
                    headers={"X-Admin-Token": config.ADMIN_TOKEN}).status_code == 400
    assert api.post("/admin/risk/1/decision", json={"decision": "nope"}, headers=ah()).status_code == 422
    assert api.post("/admin/risk/999999999/decision", json={"decision": "release"}, headers=ah()).status_code == 404
    assert api.get("/admin/risk/queue").status_code == 401
