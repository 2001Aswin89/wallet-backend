"""Idempotency (Milestone 3).

The key row is inserted as the FIRST statement of the same transaction that does the work, so
the key, the transfer and the saved response commit (or roll back) together:

* New key              -> we own the row, run the work, save the response on it, COMMIT.
* Same key, concurrent -> our INSERT blocks on the unique index until the first transaction
                          finishes. If it committed, we see the conflict and replay its response;
                          if it rolled back, our INSERT succeeds and we do the work.
* Same key, other body -> 422 IDEMPOTENCY_KEY_REUSED, nothing runs.
* Failed request       -> the exception rolls the key back too, so a retry is evaluated afresh
                          (documented assumption; a 4xx is not "the first response").

Callers with no user (POST /users) are scoped by the partial unique index on `key WHERE user_id IS NULL`.
"""
import hashlib
import json
import uuid
from typing import Annotated, Callable

import psycopg.errors
from fastapi import Header, Response

from app import db
from app.errors import AppError


def idempotency_key(idempotency_key: Annotated[str | None, Header()] = None) -> str:
    if idempotency_key is None or not idempotency_key.strip():
        raise AppError(400, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key header (a UUID) is required on every POST")
    try:
        return str(uuid.UUID(idempotency_key.strip()))
    except ValueError:
        raise AppError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be a UUID")


def request_hash(method: str, path: str, body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{method} {path} {canonical}".encode()).hexdigest()


def render(status: int, body: dict) -> Response:
    """First response and every replay go through this, so the bytes are identical."""
    return Response(content=json.dumps(body, sort_keys=True, separators=(",", ":")),
                    status_code=status, media_type="application/json")


def _claim(conn, user_id: int | None, key: str, req_hash: str, endpoint: str):
    if user_id is None:
        sql = ("INSERT INTO idempotency_keys (user_id, key, request_hash, endpoint) VALUES (NULL, %s, %s, %s) "
               "ON CONFLICT (key) WHERE user_id IS NULL DO NOTHING RETURNING id")
        params = (key, req_hash, endpoint)
    else:
        sql = ("INSERT INTO idempotency_keys (user_id, key, request_hash, endpoint) VALUES (%s, %s, %s, %s) "
               "ON CONFLICT (user_id, key) DO NOTHING RETURNING id")
        params = (user_id, key, req_hash, endpoint)
    try:
        return conn.execute(sql, params).fetchone()
    except psycopg.errors.ForeignKeyViolation:
        raise AppError(401, "UNAUTHENTICATED", "unknown user")


def run_idempotent(*, user_id: int | None, key: str, method: str, path: str, body: dict,
                   work: Callable[..., tuple[int, dict]]) -> Response:
    """work(conn) -> (status_code, response_body). Runs inside the transaction that holds the key."""
    req_hash = request_hash(method, path, body)
    with db.tx() as conn:                                   # BEGIN
        mine = _claim(conn, user_id, key, req_hash, f"{method} {path}")
        if mine is None:
            existing = conn.execute(
                "SELECT request_hash, status, response_code, response_body FROM idempotency_keys "
                "WHERE user_id IS NOT DISTINCT FROM %s AND key = %s", (user_id, key),
            ).fetchone()
            if existing["request_hash"] != req_hash:
                raise AppError(422, "IDEMPOTENCY_KEY_REUSED", "this Idempotency-Key was used with a different request")
            if existing["status"] != "completed":
                raise AppError(409, "REQUEST_IN_PROGRESS", "the original request is still running; retry shortly")
            return render(existing["response_code"], existing["response_body"])

        status, resp = work(conn)
        conn.execute(
            "UPDATE idempotency_keys SET status = 'completed', response_code = %s, response_body = %s, "
            "transfer_id = %s WHERE id = %s",
            (status, json.dumps(resp), resp.get("transfer_id"), mine["id"]),
        )
        return render(status, resp)
    # COMMIT on leaving the block: key + work + saved response become visible together.
