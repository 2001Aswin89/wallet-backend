import re
from typing import Annotated, Literal

import psycopg.errors
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, StrictStr

from app import db
from app.auth import current_user_id
from app.errors import AppError

router = APIRouter()

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
PHONE_RE = r"^\+?[0-9]{8,15}$"


class UserIn(BaseModel):
    name: StrictStr = Field(min_length=1, max_length=150)
    email: StrictStr = Field(max_length=254, pattern=EMAIL_RE)
    phone: StrictStr = Field(pattern=PHONE_RE)
    kyc_status: Literal["pending", "verified", "rejected"] = "pending"


class WalletIn(BaseModel):
    label: StrictStr = Field(default="main", min_length=1, max_length=50)


@router.post("/users", status_code=201)
def create_user(body: UserIn):
    try:
        with db.tx() as conn:
            row = conn.execute(
                "INSERT INTO users (name, email, phone, kyc_status) VALUES (%s, %s, %s, %s) "
                "RETURNING id, name, email, phone, kyc_status, created_at",
                (body.name, body.email, body.phone, body.kyc_status),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise AppError(409, "USER_EXISTS", "a user with this email or phone already exists")
    row["created_at"] = row["created_at"].isoformat()
    return row


@router.post("/users/{user_id}/wallets", status_code=201)
def open_wallet(user_id: int, body: WalletIn, caller: Annotated[int, Depends(current_user_id)]):
    if caller != user_id:
        raise AppError(403, "FORBIDDEN", "you can only open wallets for yourself")
    try:
        with db.tx() as conn:
            if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
                raise AppError(404, "USER_NOT_FOUND", "user does not exist")
            row = conn.execute(
                "INSERT INTO accounts (account_type, user_id, label) VALUES ('user_wallet', %s, %s) "
                "RETURNING id, user_id, label, currency, balance, created_at",
                (user_id, body.label),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise AppError(409, "WALLET_EXISTS", "you already have a wallet with this label")
    row["created_at"] = row["created_at"].isoformat()
    return row
