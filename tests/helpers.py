import uuid

import httpx

from app import db


def new_user(http: httpx.Client, name: str = "Test", kyc: str = "verified") -> int:
    n = uuid.uuid4().int % 10**10
    r = http.post("/users", json={"name": name, "email": f"u{uuid.uuid4().hex}@example.com",
                                  "phone": f"+91{n:010d}", "kyc_status": kyc},
                  headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def new_wallet(http: httpx.Client, user_id: int, label: str = "main") -> int:
    r = http.post(f"/users/{user_id}/wallets", json={"label": label},
                  headers={"X-User-Id": str(user_id), "Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def topup(http: httpx.Client, user_id: int, wallet_id: int, amount: int) -> httpx.Response:
    return http.post("/transfers/topup", json={"wallet_id": wallet_id, "amount": amount},
                     headers={"X-User-Id": str(user_id), "Idempotency-Key": str(uuid.uuid4())})


def funded_wallet(http: httpx.Client, amount: int, name: str = "Test") -> tuple[int, int]:
    uid = new_user(http, name)
    wid = new_wallet(http, uid)
    if amount:
        assert topup(http, uid, wid, amount).status_code == 201
    return uid, wid


def ledger_totals(conn) -> tuple[int, int]:
    r = conn.execute(
        "SELECT COALESCE(SUM(amount) FILTER (WHERE direction='debit'),0) AS d, "
        "COALESCE(SUM(amount) FILTER (WHERE direction='credit'),0) AS c FROM ledger_entries"
    ).fetchone()
    return r["d"], r["c"]


def computed_balance(conn, wallet_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(CASE direction WHEN 'credit' THEN amount ELSE -amount END),0) AS b "
        "FROM ledger_entries WHERE account_id = %s", (wallet_id,)
    ).fetchone()["b"]


def stored_balance(conn, wallet_id: int) -> int:
    return conn.execute("SELECT balance FROM accounts WHERE id=%s", (wallet_id,)).fetchone()["balance"]
