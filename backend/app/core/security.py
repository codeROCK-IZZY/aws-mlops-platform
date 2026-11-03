"""Password hashing (argon2id) and token helpers.

* Access tokens: short-lived signed JWTs carried in the Authorization header.
* Refresh tokens: random opaque strings sent as an httpOnly cookie; only their
  SHA-256 hash is stored, so a database leak does not leak usable tokens.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings

_hasher = PasswordHasher()  # argon2id with library defaults (OWASP-compliant)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# A dummy hash used to spend the same time verifying when the user doesn't exist,
# which avoids revealing valid e-mail addresses through response timing.
DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=s.access_token_minutes),
        "type": "access",
    }
    return jwt.encode(claims, s.signing_key(), algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises ``jwt.PyJWTError`` if invalid or expired."""
    s = get_settings()
    claims: dict[str, Any] = jwt.decode(
        token,
        s.signing_key(),
        algorithms=[s.jwt_algorithm],
        options={"require": ["exp", "sub", "type"]},
    )
    if claims.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return claims


def new_refresh_token() -> tuple[str, str]:
    """Returns ``(raw_token, sha256_hash)``. Only the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
