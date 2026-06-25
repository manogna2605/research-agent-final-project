"""
JWT auth — register, login, token validation, user key lookup.
"""
import os
from datetime import datetime, timedelta

from fastapi import Header, HTTPException
from jose import JWTError, jwt
from passlib.context import CryptContext

from .database import get_db

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-secret-change-this-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 60  # stay logged in for 60 days

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")


def get_current_user(authorization: str = Header(None)) -> dict:
    """FastAPI dependency — validates the Bearer token and returns the user row."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated.")
    user_id = _decode_token(authorization.split(" ", 1)[1])
    db = get_db()
    row = db.execute(
        "SELECT id, username, email, created_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    db.close()
    if not row:
        raise HTTPException(status_code=401, detail="Account no longer exists.")
    return dict(row)


def get_user_keys(user_id: int) -> dict:
    """Returns the api_keys row as a dict, or empty dict if not configured yet."""
    db = get_db()
    row = db.execute("SELECT * FROM api_keys WHERE user_id = ?", (user_id,)).fetchone()
    db.close()
    return dict(row) if row else {}
