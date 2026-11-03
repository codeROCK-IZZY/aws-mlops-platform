"""Shared FastAPI dependencies: DB session, current user, role guards.

Role hierarchy: admin > approver > engineer > viewer. ``require_role("engineer")`` admits
engineers, approvers and admins.
"""

import uuid
from collections.abc import Callable, Iterator
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import Forbidden, Unauthorized
from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import new_session

ROLE_RANK = {"viewer": 1, "engineer": 2, "approver": 3, "admin": 4}

_bearer = HTTPBearer(auto_error=False)


def get_db() -> Iterator[Session]:
    """One session per request. Services call ``commit()`` explicitly; anything
    left uncommitted is rolled back when the request ends."""
    session = new_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


DB = Annotated[Session, Depends(get_db)]


def get_current_user(db: DB, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise Unauthorized("Missing bearer token")
    try:
        claims = decode_access_token(creds.credentials)
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise Unauthorized("Invalid or expired token") from exc
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("User is inactive or no longer exists")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: str) -> Callable[[User], User]:
    if minimum not in ROLE_RANK:
        raise ValueError(f"unknown role {minimum}")

    def _guard(user: CurrentUser) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[minimum]:
            raise Forbidden(f"Requires role '{minimum}' or higher")
        return user

    return _guard


Viewer = Annotated[User, Depends(require_role("viewer"))]
Engineer = Annotated[User, Depends(require_role("engineer"))]
Approver = Annotated[User, Depends(require_role("approver"))]
Admin = Annotated[User, Depends(require_role("admin"))]
