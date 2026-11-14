"""Login and current-user endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.security import create_access_token, verify_password
from app.db import get_db
from app.models import User
from app.schemas import APIModel

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(APIModel):
    username: str
    password: str


class UserOut(APIModel):
    username: str
    display_name: str
    role: str
    astronaut_id: str | None


class TokenOut(APIModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def _login(db: Session, username: str, password: str) -> TokenOut:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.active or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token, expires = create_access_token(user.username, user.role, user.astronaut_id)
    return TokenOut(access_token=token, expires_in=expires, user=UserOut.model_validate(user, from_attributes=True))


@router.post("/login", response_model=TokenOut, summary="Log in with JSON (for the apps)")
def login(body: LoginIn, db: Session = Depends(get_db)):
    return _login(db, body.username, body.password)


@router.post("/token", summary="OAuth2 password flow (used by the Authorize button in /docs)")
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    result = _login(db, form.username, form.password)
    # OAuth2 clients expect snake_case keys here.
    return {"access_token": result.access_token, "token_type": "bearer", "expires_in": result.expires_in}


@router.get("/me", response_model=UserOut, summary="Current user")
def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user, from_attributes=True)
