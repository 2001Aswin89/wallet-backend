from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app import db
from app.auth import current_user_id
from app.errors import AppError

router = APIRouter()


def _own_wallet(conn, wallet_id: int, caller: int) -> dict:
    w = conn.execute(
        "SELECT id, user_id, label, currency, balance FROM accounts "
        "WHERE id = %s AND account_type = 'user_wallet'",
        (wallet_id,),
    ).fetchone()
    if w is None:
        raise AppError(404, "WALLET_NOT_FOUND", "wallet does not exist")
    if w["user_id"] != caller:
        raise AppError(403, "FORBIDDEN", "wallet belongs to another user")
    return w


@router.get("/wallets/{wallet_id}/balance")
def balance(wallet_id: int, caller: Annotated[int, Depends(current_user_id)]):
    with db.tx() as conn:
        w = _own_wallet(conn, wallet_id, caller)
    return {"wallet_id": w["id"], "balance": w["balance"], "currency": w["currency"]}


@router.get("/wallets/{wallet_id}/statement")
def statement(
    wallet_id: int,
    caller: Annotated[int, Depends(current_user_id)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    with db.tx() as conn:
        _own_wallet(conn, wallet_id, caller)
        rows = conn.execute(
            "SELECT le.id, le.transfer_id, le.direction, le.amount, le.created_at, t.type, t.status, t.note "
            "FROM ledger_entries le JOIN transfers t ON t.id = le.transfer_id "
            "WHERE le.account_id = %s "
            "ORDER BY le.created_at DESC, le.id DESC LIMIT %s OFFSET %s",
            (wallet_id, limit, offset),
        ).fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return {"wallet_id": wallet_id, "limit": limit, "offset": offset, "entries": rows}
