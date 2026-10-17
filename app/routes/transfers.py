import hmac
from typing import Annotated

import psycopg.errors
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field, StrictInt, StrictStr

from app import config, db
from app.auth import current_user_id, require_admin
from app.errors import AppError
from app.services.idempotency import idempotency_key, run_idempotent
from app.services.transfer import execute_reversal, execute_transfer

router = APIRouter()

# Paise. Upper bound keeps a typo from becoming a crore-sized transfer.
Amount = Annotated[StrictInt, Field(gt=0, le=10_000_000_000)]
WalletId = Annotated[StrictInt, Field(gt=0)]

Caller = Annotated[int, Depends(current_user_id)]
Key = Annotated[str, Depends(idempotency_key)]


class TopUpIn(BaseModel):
    wallet_id: WalletId
    amount: Amount


class PeerIn(BaseModel):
    from_wallet_id: WalletId
    to_wallet_id: WalletId
    amount: Amount
    note: StrictStr | None = Field(default=None, max_length=255)


class ReverseIn(BaseModel):
    note: StrictStr | None = Field(default=None, max_length=255)


class WithdrawIn(BaseModel):
    wallet_id: WalletId
    amount: Amount


@router.post("/transfers/topup", status_code=201)
def topup(body: TopUpIn, caller: Caller, key: Key):
    def work(conn):
        return 201, execute_transfer(conn, kind="top_up", caller_id=caller, wallet_id=body.wallet_id,
                                     other_wallet_id=None, amount=body.amount)
    return run_idempotent(user_id=caller, key=key, method="POST", path="/transfers/topup",
                          body=body.model_dump(), work=work)


@router.post("/transfers", status_code=201)
def peer_transfer(body: PeerIn, caller: Caller, key: Key):
    if body.from_wallet_id == body.to_wallet_id:
        raise AppError(422, "VALIDATION_ERROR", "from_wallet_id and to_wallet_id must differ")

    def work(conn):
        return 201, execute_transfer(conn, kind="peer_transfer", caller_id=caller, wallet_id=body.from_wallet_id,
                                     other_wallet_id=body.to_wallet_id, amount=body.amount, note=body.note)
    return run_idempotent(user_id=caller, key=key, method="POST", path="/transfers",
                          body=body.model_dump(), work=work)


@router.post("/transfers/{transfer_id}/reverse", status_code=201)
def reverse(transfer_id: int, body: ReverseIn, key: Key, admin: Annotated[str, Depends(require_admin)]):
    """Admin only (assumption): undoing money another user received is a privileged operation.
    Admin calls have no user row, so their idempotency keys use the user_id IS NULL scope."""
    def work(conn):
        return 201, execute_reversal(conn, admin_actor=admin, original_id=transfer_id, note=body.note)

    try:
        return run_idempotent(user_id=None, key=key, method="POST", path=f"/transfers/{transfer_id}/reverse",
                              body=body.model_dump(), work=work)
    except psycopg.errors.UniqueViolation:  # backstop: UNIQUE (reverses_transfer_id)
        raise AppError(409, "REVERSAL_NOT_POSSIBLE", "this transfer has already been reversed")


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int, x_user_id: Annotated[str | None, Header()] = None,
                 x_admin_token: Annotated[str | None, Header()] = None):
    """Visible to an admin, or to the owner of the sending or receiving wallet."""
    is_admin = x_admin_token is not None and hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN)
    if not is_admin and (x_user_id is None or not x_user_id.isdigit()):
        raise AppError(401, "UNAUTHENTICATED", "X-User-Id or X-Admin-Token header is required")
    with db.tx() as conn:
        t = conn.execute(
            "SELECT t.id AS transfer_id, t.type, t.status, t.amount, t.from_account_id, t.to_account_id, "
            "t.initiated_by, t.reverses_transfer_id, t.note, t.created_at, t.completed_at, "
            "fa.user_id AS from_user_id, ta.user_id AS to_user_id, "
            "(SELECT r.id FROM transfers r WHERE r.reverses_transfer_id = t.id) AS reversed_by_transfer_id "
            "FROM transfers t JOIN accounts fa ON fa.id = t.from_account_id "
            "JOIN accounts ta ON ta.id = t.to_account_id WHERE t.id = %s", (transfer_id,),
        ).fetchone()
        if t is None:
            raise AppError(404, "TRANSFER_NOT_FOUND", "transfer does not exist")
        if not is_admin and int(x_user_id) not in (t["from_user_id"], t["to_user_id"]):
            raise AppError(403, "FORBIDDEN", "transfer does not involve your wallets")
        entries = conn.execute(
            "SELECT id, account_id, direction, amount, created_at FROM ledger_entries "
            "WHERE transfer_id = %s ORDER BY id", (transfer_id,)).fetchall()
        history = conn.execute(
            "SELECT from_status, to_status, changed_by, reason, changed_at FROM transfer_status_history "
            "WHERE transfer_id = %s ORDER BY changed_at, id", (transfer_id,)).fetchall()
    for row in (t, *entries, *history):
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()
    del t["from_user_id"], t["to_user_id"]
    return {**t, "entries": entries, "status_history": history}


@router.post("/transfers/withdraw", status_code=201)
def withdraw(body: WithdrawIn, caller: Caller, key: Key):
    def work(conn):
        return 201, execute_transfer(conn, kind="withdrawal", caller_id=caller, wallet_id=body.wallet_id,
                                     other_wallet_id=None, amount=body.amount)
    return run_idempotent(user_id=caller, key=key, method="POST", path="/transfers/withdraw",
                          body=body.model_dump(), work=work)
