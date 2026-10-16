import uuid

from app import db
from tests.helpers import funded_wallet, new_user, new_wallet, computed_balance, stored_balance, topup


def h(uid):
    return {"X-User-Id": str(uid), "Idempotency-Key": str(uuid.uuid4())}


def test_topup_balance_statement(client):
    uid, wid = funded_wallet(client, 50_000)
    r = client.get(f"/wallets/{wid}/balance", headers=h(uid))
    assert r.json()["balance"] == 50_000
    s = client.get(f"/wallets/{wid}/statement", headers=h(uid)).json()
    assert len(s["entries"]) == 1
    assert s["entries"][0]["direction"] == "credit" and s["entries"][0]["type"] == "top_up"


def test_peer_transfer_moves_money_and_statement_is_newest_first(client):
    a, wa = funded_wallet(client, 100_000)
    b, wb = funded_wallet(client, 0)
    for amt in (100, 200, 300):
        r = client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": amt}, headers=h(a))
        assert r.status_code == 201, r.text
    assert client.get(f"/wallets/{wa}/balance", headers=h(a)).json()["balance"] == 99_400
    assert client.get(f"/wallets/{wb}/balance", headers=h(b)).json()["balance"] == 600
    amounts = [e["amount"] for e in client.get(f"/wallets/{wa}/statement", headers=h(a)).json()["entries"]]
    assert amounts == [300, 200, 100, 100_000]
    page = client.get(f"/wallets/{wa}/statement?limit=2&offset=1", headers=h(a)).json()["entries"]
    assert [e["amount"] for e in page] == [200, 100]
    with db.tx() as conn:
        assert stored_balance(conn, wa) == computed_balance(conn, wa)


def test_withdraw(client):
    uid, wid = funded_wallet(client, 10_000)
    r = client.post("/transfers/withdraw", json={"wallet_id": wid, "amount": 4_000}, headers=h(uid))
    assert r.status_code == 201
    assert client.get(f"/wallets/{wid}/balance", headers=h(uid)).json()["balance"] == 6_000


def test_insufficient_funds_leaves_no_trace(client):
    a, wa = funded_wallet(client, 500)
    b, wb = funded_wallet(client, 0)
    r = client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": 501}, headers=h(a))
    assert r.status_code == 422 and r.json()["code"] == "INSUFFICIENT_FUNDS"
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM transfers WHERE from_account_id=%s", (wa,)).fetchone()["n"]
        assert n == 0
        assert stored_balance(conn, wa) == 500


def test_ownership_and_validation(client):
    a, wa = funded_wallet(client, 1_000)
    b, wb = funded_wallet(client, 0)
    # b cannot spend a's wallet or read its balance
    r = client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": 10}, headers=h(b))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert client.get(f"/wallets/{wa}/balance", headers=h(b)).status_code == 403
    # b cannot top up a's wallet
    assert topup(client, b, wa, 10).status_code == 403
    # missing auth, bad amounts, unknown wallet, self-transfer
    assert client.get(f"/wallets/{wa}/balance").status_code == 401
    for bad in (0, -5, 1.5, "10", True):
        r = client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": bad}, headers=h(a))
        assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR", bad
    assert client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": 99_999_999, "amount": 10},
                       headers=h(a)).status_code == 404
    assert client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wa, "amount": 10},
                       headers=h(a)).status_code == 422
    # cannot send into a system account by passing its id
    with db.tx() as conn:
        cash_out = conn.execute("SELECT id FROM accounts WHERE system_code='CASH_OUT'").fetchone()["id"]
    assert client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": cash_out, "amount": 10},
                       headers=h(a)).status_code == 404


def test_duplicate_user_and_wallet(client):
    uid = new_user(client)
    new_wallet(client, uid, "savings")
    r = client.post(f"/users/{uid}/wallets", json={"label": "savings"}, headers=h(uid))
    assert r.status_code == 409 and r.json()["code"] == "WALLET_EXISTS"
    body = {"name": "X", "email": "dup@example.com", "phone": "+919999900001"}
    client.post("/users", json=body)
    r = client.post("/users", json={**body, "phone": "+919999900002"})  # same email
    assert r.status_code == 409 and r.json()["code"] == "USER_EXISTS"


def test_rejected_kyc_cannot_move_money(client):
    uid = new_user(client, kyc="rejected")
    wid = new_wallet(client, uid)
    r = topup(client, uid, wid, 100)
    assert r.status_code == 403 and r.json()["code"] == "KYC_REJECTED"
