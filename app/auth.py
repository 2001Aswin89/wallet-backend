"""Auth is not specified by the brief. Assumption: X-User-Id for user endpoints, X-Admin-Token for /admin/*."""
import hmac
from typing import Annotated

from fastapi import Header

from app import config
from app.errors import AppError


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> int:
    if x_user_id is None or not x_user_id.isdigit():
        raise AppError(401, "UNAUTHENTICATED", "X-User-Id header (a numeric user id) is required")
    return int(x_user_id)


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> str:
    if x_admin_token is None or not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise AppError(401, "UNAUTHENTICATED", "valid X-Admin-Token header is required")
    return "admin:1"
