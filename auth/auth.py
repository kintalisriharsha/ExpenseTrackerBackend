from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from typing import Optional
import logging, os, uuid

logger = logging.getLogger(__name__)
load_dotenv()

SECRET_KEY                  = os.getenv("SECRET_KEY")
ALGORITHM                   = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 15))
REFRESH_TOKEN_EXPIRE_DAYS   = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS",   7))

if not SECRET_KEY:
    raise ValueError("SECRET_KEY is not set in .env")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/email/verify-otp")


# ── Token creation ─────────────────────────────────────────────────────────────

def create_access_token(user: object) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub":            str(user.id),
        "email":          user.email,
        "daily_budget":   float(user.daily_budget   or 0.0),
        "monthly_budget": float(user.monthly_budget or 0.0),
        "type":           "access",
        "exp":            expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user: object) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub":  str(user.id),
        "jti":  str(uuid.uuid4()),
        "type": "refresh",
        "exp":  expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ── Token decoding ─────────────────────────────────────────────────────────────

def decode_token(token: str, expected_type: str = "access") -> dict:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != expected_type:
            raise exc
        if not payload.get("sub"):
            raise exc
        return payload
    except JWTError:
        raise exc


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    payload = decode_token(token, expected_type="access")
    return {
        "id":             int(payload["sub"]),
        "email":          payload.get("email"),
        "daily_budget":   float(payload.get("daily_budget",   0.0)),
        "monthly_budget": float(payload.get("monthly_budget", 0.0)),
    }
