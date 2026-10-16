from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, StrictInt, StrictStr

from app import db
from app.auth import current_user_id
from app.errors import AppError
from app.services.transfer import execute_transfer

router = APIRouter()

# Paise. Upper bound keeps a typo from becoming a crore-sized transfer.
Amount = Annotated[StrictInt, Field(gt=0, le=10_000_000_000)]
WalletId = Annotated[StrictInt, Field(gt=0)]


class TopUpIn(BaseModel):
    wallet_id: WalletId
    amount: Amount


class PeerIn(BaseModel):
    from_wallet_id: WalletId
    to_wallet_id: WalletId
    amount: Amount
    note: StrictStr | None = Field(default=None, max_length=255)


class WithdrawIn(BaseModel):
    wallet_id: WalletId
    amount: Amount


@router.post("/transfers/topup", status_code=201)
def topup(body: TopUpIn, caller: Annotated[int, Depends(current_user_id)]):
    with db.tx() as conn:
        return execute_transfer(conn, kind="top_up", caller_id=caller, wallet_id=body.wallet_id,
                                other_wallet_id=None, amount=body.amount)


@router.post("/transfers", status_code=201)
def peer_transfer(body: PeerIn, caller: Annotated[int, Depends(current_user_id)]):
    if body.from_wallet_id == body.to_wallet_id:
        raise AppError(422, "VALIDATION_ERROR", "from_wallet_id and to_wallet_id must differ")
    with db.tx() as conn:
        return execute_transfer(conn, kind="peer_transfer", caller_id=caller, wallet_id=body.from_wallet_id,
                                other_wallet_id=body.to_wallet_id, amount=body.amount, note=body.note)


@router.post("/transfers/withdraw", status_code=201)
def withdraw(body: WithdrawIn, caller: Annotated[int, Depends(current_user_id)]):
    with db.tx() as conn:
        return execute_transfer(conn, kind="withdrawal", caller_id=caller, wallet_id=body.wallet_id,
                                other_wallet_id=None, amount=body.amount)
