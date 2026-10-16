from typing import Annotated, Literal

import psycopg.errors
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, StrictStr

from app.auth import current_user_id
from app.errors import AppError
from app.services.idempotency import idempotency_key, run_idempotent

router = APIRouter()

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
PHONE_RE = r"^\+?[0-9]{8,15}$"

Key = Annotated[str, Depends(idempotency_key)]


class UserIn(BaseModel):
    name: StrictStr = Field(min_length=1, max_length=150)
    email: StrictStr = Field(max_length=254, pattern=EMAIL_RE)
    phone: StrictStr = Field(pattern=PHONE_RE)
    kyc_status: Literal["pending", "verified", "rejected"] = "pending"


class WalletIn(BaseModel):
    label: StrictStr = Field(default="main", min_length=1, max_length=50)


@router.post("/users", status_code=201)
def create_user(body: UserIn, key: Key):
    def work(conn):
        row = conn.execute(
            "INSERT INTO users (name, email, phone, kyc_status) VALUES (%s, %s, %s, %s) "
            "RETURNING id, name, email, phone, kyc_status, created_at",
            (body.name, body.email, body.phone, body.kyc_status),
        ).fetchone()
        row["created_at"] = row["created_at"].isoformat()
        return 201, row

    try:
        # No user exists yet, so this key is scoped by the partial unique index (user_id IS NULL).
        return run_idempotent(user_id=None, key=key, method="POST", path="/users",
                              body=body.model_dump(), work=work)
    except psycopg.errors.UniqueViolation:
        raise AppError(409, "USER_EXISTS", "a user with this email or phone already exists")


@router.post("/users/{user_id}/wallets", status_code=201)
def open_wallet(user_id: int, body: WalletIn, caller: Annotated[int, Depends(current_user_id)], key: Key):
    if caller != user_id:
        raise AppError(403, "FORBIDDEN", "you can only open wallets for yourself")

    def work(conn):
        row = conn.execute(
            "INSERT INTO accounts (account_type, user_id, label) VALUES ('user_wallet', %s, %s) "
            "RETURNING id, user_id, label, currency, balance, created_at",
            (user_id, body.label),
        ).fetchone()
        row["created_at"] = row["created_at"].isoformat()
        return 201, row

    try:
        return run_idempotent(user_id=caller, key=key, method="POST", path=f"/users/{user_id}/wallets",
                              body=body.model_dump(), work=work)
    except psycopg.errors.UniqueViolation:
        raise AppError(409, "WALLET_EXISTS", "you already have a wallet with this label")
