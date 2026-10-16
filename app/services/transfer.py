"""THE money path. Raw SQL, one explicit transaction (opened by the caller via db.tx()),
Read Committed + SELECT ... FOR UPDATE on both account rows in ascending id order.

Transaction boundaries: BEGIN is the start of the caller's `with db.tx()`; COMMIT happens when
that block exits cleanly; any exception (InsufficientFunds, constraint failure, ...) rolls back
the transfer, its entries and its balance updates together.
"""
from app.errors import AppError

_system_ids: dict[str, int] = {}


def system_account_id(conn, code: str) -> int:
    """System account ids never change after migration, so they are cached per process."""
    if code not in _system_ids:
        row = conn.execute("SELECT id FROM accounts WHERE system_code = %s", (code,)).fetchone()
        _system_ids[code] = row["id"]
    return _system_ids[code]


def insufficient_funds() -> AppError:
    return AppError(422, "INSUFFICIENT_FUNDS", "wallet balance is too low for this transfer")


def execute_transfer(conn, *, kind: str, caller_id: int, wallet_id: int, other_wallet_id: int | None,
                     amount: int, note: str | None = None) -> dict:
    """kind: 'top_up' (CASH_IN -> wallet_id), 'peer_transfer' (wallet_id -> other_wallet_id),
    'withdrawal' (wallet_id -> CASH_OUT). Returns the response body for the new transfer."""
    conn.execute("SELECT set_config('app.actor', %s, true)", (f"user:{caller_id}",))

    if kind == "top_up":
        from_id, to_id = system_account_id(conn, "CASH_IN"), wallet_id
    elif kind == "peer_transfer":
        from_id, to_id = wallet_id, other_wallet_id
    elif kind == "withdrawal":
        from_id, to_id = wallet_id, system_account_id(conn, "CASH_OUT")
    else:
        raise ValueError(kind)

    # 1. Lock both rows, ASCENDING id order. Two opposite transfers (A->B, B->A) queue on the
    #    lower id instead of deadlocking. LockRows sits above Sort, so locks follow the ORDER BY.
    ids = sorted({from_id, to_id})
    locked = conn.execute(
        "SELECT id, account_type, user_id, balance FROM accounts "
        "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
        (ids,),
    ).fetchall()
    accts = {r["id"]: r for r in locked}
    if len(accts) != len(ids):
        raise AppError(404, "WALLET_NOT_FOUND", "wallet does not exist")
    src, dst = accts[from_id], accts[to_id]

    # 2. Checks against the LOCKED rows (nobody can change them until we commit).
    owned = src if kind in ("peer_transfer", "withdrawal") else dst
    if owned["account_type"] != "user_wallet":
        raise AppError(404, "WALLET_NOT_FOUND", "wallet does not exist")
    if owned["user_id"] != caller_id:
        raise AppError(403, "FORBIDDEN", "wallet belongs to another user")
    if kind == "peer_transfer" and dst["account_type"] != "user_wallet":
        raise AppError(404, "WALLET_NOT_FOUND", "destination wallet does not exist")
    kyc = conn.execute("SELECT kyc_status FROM users WHERE id = %s", (caller_id,)).fetchone()
    if kyc is None or kyc["kyc_status"] == "rejected":
        raise AppError(403, "KYC_REJECTED", "user is not allowed to move money")

    # 3. Re-read balance under the lock (the value we just locked), reject if short.
    if src["account_type"] == "user_wallet" and src["balance"] < amount:
        raise insufficient_funds()

    # 4. Transfer row, balanced ledger entries, stored balances.
    t = conn.execute(
        "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, initiated_by, note, completed_at) "
        "VALUES (%s, 'completed', %s, %s, %s, %s, %s, now()) RETURNING id, created_at",
        (kind, amount, from_id, to_id, caller_id, note),
    ).fetchone()
    conn.execute(
        "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
        "VALUES (%s, %s, 'debit', %s), (%s, %s, 'credit', %s)",
        (t["id"], from_id, amount, t["id"], to_id, amount),
    )
    conn.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s", (amount, from_id))
    conn.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (amount, to_id))

    return {
        "transfer_id": t["id"],
        "type": kind,
        "status": "completed",
        "amount": amount,
        "from_account_id": from_id,
        "to_account_id": to_id,
        "note": note,
        "created_at": t["created_at"].isoformat(),
    }
    # COMMIT happens in the caller when the `with db.tx()` block exits.
