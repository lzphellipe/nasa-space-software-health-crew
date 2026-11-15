"""FastAPI dependencies for authentication and per-astronaut access control.

Roles:
* astronaut: only their own astronaut's data (check-ins, daily records, messages, support);
* medical: the whole crew, the support queue and the knowledge base uploads;
* admin: everything medical can do.
"""

from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import decode_token
from app.db import get_db
from app.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

CARE_ROLES = ("medical", "admin")


def get_current_user(token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"}
    )
    if not token:
        raise unauthorized
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise unauthorized from None
    user = db.scalar(select(User).where(User.username == payload.get("sub")))
    if user is None or not user.active:
        raise unauthorized
    return user


def require_roles(*roles: str):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed for this role")
        return user

    return checker


require_care_team = require_roles(*CARE_ROLES)


def require_astronaut(user: User = Depends(get_current_user)) -> User:
    if user.role != "astronaut" or not user.astronaut_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only astronaut accounts have personal endpoints")
    return user


def assert_can_access(user: User, astronaut_id: str) -> None:
    """Astronauts may only see their own data; the care team sees everyone."""
    if user.role in CARE_ROLES:
        return
    if user.role == "astronaut" and user.astronaut_id and user.astronaut_id.upper() == astronaut_id.upper():
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only access your own data")
