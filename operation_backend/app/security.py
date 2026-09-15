from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import (
    ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    OP_JWT_EXPIRE_HOURS,
    SECRET_KEY,
)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except Exception:
        return False


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_op_token(
    *,
    user_id: int,
    op_role: str,
    dept_id: int | None,
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=OP_JWT_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "op_role": op_role,
        "dept_id": dept_id,
        "aud": JWT_AUDIENCE,
        "iss": JWT_ISSUER,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_op_token(token: str) -> dict:
    return jwt.decode(
        token,
        SECRET_KEY,
        algorithms=[ALGORITHM],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
    )


def safe_decode_op_token(token: str) -> dict | None:
    try:
        return decode_op_token(token)
    except JWTError:
        return None
