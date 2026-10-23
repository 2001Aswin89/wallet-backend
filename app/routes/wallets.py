import base64
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app import db
from app.auth import current_user_id
from app.errors import AppError

router = APIRouter()

COLUMNS = ("le.id, le.transfer_id, le.direction, le.amount, le.created_at, t.type, t.status, t.note "
           "FROM ledger_entries le JOIN transfers t ON t.id = le.transfer_id WHERE le.account_id = %s")


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


def _encode_cursor(created_at: datetime, entry_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at.isoformat(), entry_id]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        ts, entry_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(ts), int(entry_id)
    except (ValueError, TypeError, KeyError):
        raise AppError(422, "VALIDATION_ERROR", "cursor is not valid")


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
    cursor: Annotated[str | None, Query(max_length=200)] = None,
):
    """Newest first. Two paging styles over the same ordering (created_at DESC, id DESC):
    LIMIT/OFFSET (the brief's contract; cost grows with the offset) and `cursor` keyset paging
    (seeks straight into idx_ledger_account_time; cost stays flat). Use `next_cursor` to continue."""
    if cursor is not None and offset:
        raise AppError(422, "VALIDATION_ERROR", "use either offset or cursor, not both")
    with db.tx() as conn:
        _own_wallet(conn, wallet_id, caller)
        if cursor is None:
            rows = conn.execute(
                "SELECT " + COLUMNS + " ORDER BY le.created_at DESC, le.id DESC LIMIT %s OFFSET %s",
                (wallet_id, limit, offset)).fetchall()
        else:
            ts, last_id = _decode_cursor(cursor)
            rows = conn.execute(
                "SELECT " + COLUMNS + " AND (le.created_at, le.id) < (%s, %s) "
                "ORDER BY le.created_at DESC, le.id DESC LIMIT %s",
                (wallet_id, ts, last_id, limit)).fetchall()
    next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if len(rows) == limit else None
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return {"wallet_id": wallet_id, "limit": limit, "offset": offset, "entries": rows, "next_cursor": next_cursor}
