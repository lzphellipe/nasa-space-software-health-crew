"""Login and current-user endpoints."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import Field
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.security import create_access_token, verify_password
from app.db import get_db
from app.models import User, UserRefreshToken
from app.schemas import APIModel

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

REFRESH_TOKEN_TTL = timedelta(days=30)


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
    refresh_token: str
    refresh_expires_in: int
    user: UserOut


class RefreshIn(APIModel):
    refresh_token: str = Field(min_length=1, max_length=128)


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create_token_pair(
    db: Session,
    user: User,
    *,
    family_id: str | None = None,
    refresh_expires_at: datetime | None = None,
) -> TokenOut:
    now = datetime.now(timezone.utc)
    expires_at = refresh_expires_at or now + REFRESH_TOKEN_TTL
    refresh_token = secrets.token_urlsafe(48)
    db.execute(
        delete(UserRefreshToken)
        .where(UserRefreshToken.expires_at <= now)
        .execution_options(synchronize_session=False)
    )
    db.add(
        UserRefreshToken(
            user_id=user.id,
            token_hash=_token_hash(refresh_token),
            family_id=family_id or secrets.token_hex(16),
            expires_at=expires_at,
            created_at=now,
        )
    )
    token, expires = create_access_token(user.username, user.role, user.astronaut_id)
    refresh_expires_in = max(0, int((expires_at - now).total_seconds()))
    return TokenOut(
        access_token=token,
        expires_in=expires,
        refresh_token=refresh_token,
        refresh_expires_in=refresh_expires_in,
        user=UserOut.model_validate(user, from_attributes=True),
    )


def _revoke_family(db: Session, family_id: str, now: datetime) -> None:
    db.execute(
        update(UserRefreshToken)
        .where(UserRefreshToken.family_id == family_id, UserRefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


def _login(db: Session, username: str, password: str) -> TokenOut:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.active or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    result = _create_token_pair(db, user)
    db.commit()
    return result


@router.post("/login", response_model=TokenOut, summary="Log in with JSON (for the apps)")
def login(body: LoginIn, db: Session = Depends(get_db)):
    return _login(db, body.username, body.password)


@router.post("/token", summary="OAuth2 password flow (used by the Authorize button in /docs)")
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    result = _login(db, form.username, form.password)
    # OAuth2 clients expect snake_case keys here.
    return {
        "access_token": result.access_token,
        "token_type": result.token_type,
        "expires_in": result.expires_in,
        "refresh_token": result.refresh_token,
        "refresh_expires_in": result.refresh_expires_in,
    }


@router.post("/refresh", response_model=TokenOut, summary="Rotate a refresh token")
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    stored = db.scalar(select(UserRefreshToken).where(UserRefreshToken.token_hash == _token_hash(body.refresh_token)))
    if stored is None:
        raise _unauthorized()

    family_id = stored.family_id
    if stored.revoked_at is not None:
        _revoke_family(db, family_id, now)
        db.commit()
        raise _unauthorized()

    refresh_expires_at = _as_utc(stored.expires_at)
    if refresh_expires_at <= now:
        stored.revoked_at = now
        db.commit()
        raise _unauthorized()

    user = db.get(User, stored.user_id)
    if user is None or not user.active:
        _revoke_family(db, family_id, now)
        db.commit()
        raise _unauthorized()

    claimed = db.execute(
        update(UserRefreshToken)
        .where(
            UserRefreshToken.id == stored.id,
            UserRefreshToken.revoked_at.is_(None),
            UserRefreshToken.expires_at > now,
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        _revoke_family(db, family_id, now)
        db.commit()
        raise _unauthorized()

    result = _create_token_pair(
        db,
        user,
        family_id=family_id,
        refresh_expires_at=refresh_expires_at,
    )
    db.commit()
    return result


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke a refresh-token session")
def logout(body: RefreshIn, db: Session = Depends(get_db)):
    stored = db.scalar(select(UserRefreshToken).where(UserRefreshToken.token_hash == _token_hash(body.refresh_token)))
    if stored is None:
        raise _unauthorized()
    if stored.revoked_at is None:
        _revoke_family(db, stored.family_id, datetime.now(timezone.utc))
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut, summary="Current user")
def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user, from_attributes=True)
