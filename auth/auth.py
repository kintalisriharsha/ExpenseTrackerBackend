from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from typing import Optional
import logging
import os
import uuid

logger = logging.getLogger(__name__)

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

SECRET_KEY                  = os.getenv("SECRET_KEY")
ALGORITHM                   = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 15))
REFRESH_TOKEN_EXPIRE_DAYS   = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", 7))

if not SECRET_KEY:
    raise ValueError("SECRET_KEY is not set in .env")

# ── OAuth2 scheme ──────────────────────────────────────────────────────────────
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# ── JWT Creation ───────────────────────────────────────────────────────────────

def create_access_token(user: object) -> str:
    """
    Create a short-lived access token (15 min).

    Payload:
        sub            → user.id (Neon DB id)
        phone          → user.phone_number
        daily_budget   → user.daily_budget
        monthly_budget → user.monthly_budget
        type           → "access"
        exp            → expiry timestamp
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub":            str(user.id),
        "phone":          user.phone_number,
        "daily_budget":   float(user.daily_budget   or 0.0),
        "monthly_budget": float(user.monthly_budget or 0.0),
        "type":           "access",
        "exp":            expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user: object) -> str:
    """
    Create a long-lived refresh token (7 days).

    Now includes `jti` (JWT ID) — a unique UUID per token.
    On logout we store this jti in blacklisted_tokens instead of the full token.
    On /auth/refresh we check if this jti is blacklisted before issuing a new access token.
    """
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub":  str(user.id),
        "jti":  str(uuid.uuid4()),   # ← unique ID for this specific refresh token
        "type": "refresh",
        "exp":  expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ── JWT Decoding ───────────────────────────────────────────────────────────────

def decode_token(token: str, expected_type: str = "access") -> dict:
    """
    Decode and validate a JWT token.
    Raises HTTP 401 if invalid, expired, or wrong type.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

        if payload.get("type") != expected_type:
            raise credentials_exception

        user_id: Optional[str] = payload.get("sub")
        if not user_id:
            raise credentials_exception

        return payload

    except JWTError:
        raise credentials_exception


# ── FastAPI Dependency ─────────────────────────────────────────────────────────

async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """
    Decodes the Bearer token from Authorization header.
    Returns user info dict — no DB lookup needed for most routes.

    Usage:
        @router.get("/expenses")
        async def get_expenses(
            db: AsyncSession = Depends(get_db),
            current_user: dict = Depends(get_current_user)
        ):
            user_id = current_user["id"]
    """
    payload = decode_token(token, expected_type="access")

    return {
        "id":             int(payload.get("sub")),
        "phone":          payload.get("phone"),
        "daily_budget":   float(payload.get("daily_budget",   0.0)),
        "monthly_budget": float(payload.get("monthly_budget", 0.0)),
    }