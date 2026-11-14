"""Password hashing and JWT tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt

from app.config import get_settings

log = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 240_000
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iterations))
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


@lru_cache
def jwt_secret() -> str:
    secret = get_settings().jwt_secret
    if not secret:
        log.warning("JWT_SECRET is not set: using a random secret; tokens stop working after a restart")
        secret = secrets.token_urlsafe(48)
    elif len(secret) < 32:
        log.warning("JWT_SECRET is shorter than 32 characters; use a longer random value")
    return secret


def create_access_token(username: str, role: str, astronaut_id: str | None) -> tuple[str, int]:
    minutes = get_settings().jwt_expire_minutes
    payload = {
        "sub": username,
        "role": role,
        "astronaut_id": astronaut_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=ALGORITHM), minutes * 60


def decode_token(token: str) -> dict:
    return jwt.decode(token, jwt_secret(), algorithms=[ALGORITHM])
