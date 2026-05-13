from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from typing import Optional
import logging, os, uuid, secrets

logger = logging.getLogger(__name__)
load_dotenv()

SECRET_KEY                  = os.getenv("SECRET_KEY")
ALGORITHM                   = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 15))
REFRESH_TOKEN_EXPIRE_DAYS   = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS",   7))

if not SECRET_KEY:
    raise ValueError("SECRET_KEY is not set in .env")

# tokenUrl points to the OTP verify endpoint so Swagger UI shows the correct
# auth flow. Android ignores this — it's a documentation hint only.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/email/verify-otp")


# ── Token creation ─────────────────────────────────────────────────────────────

def create_access_token(user: object) -> str:
    """
    Access token — short-lived (15 min).
    Carries user identity + budget context so the app can work offline.
    Includes a jti so individual access tokens can be blacklisted if needed.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub":            str(user.id),
        "email":          user.email,
        "display_name":   user.display_name,
        "daily_budget":   float(user.daily_budget   or 0.0),
        "monthly_budget": float(user.monthly_budget or 0.0),
        "type":           "access",
        "jti":            str(uuid.uuid4()),   # ← added: lets us blacklist access tokens too
        "iat":            datetime.now(timezone.utc),
        "exp":            expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user: object) -> str:
    """
    Refresh token — long-lived (7 days).
    Stores minimal claims; full profile comes from the DB on each refresh.
    """
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub":  str(user.id),
        "jti":  str(uuid.uuid4()),
        "type": "refresh",
        "iat":  datetime.now(timezone.utc),
        "exp":  expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ── Token decoding ─────────────────────────────────────────────────────────────

def decode_token(token: str, expected_type: str = "access") -> dict:
    """
    Decode and validate a JWT token.
    Raises HTTP 401 on any failure — expired, wrong type, bad signature.
    """
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
    """
    Lightweight dependency — decodes the access token only (no DB hit).
    Use this on every protected route that just needs the user identity.
    For routes that need the full User ORM object, call get_user_by_id after this.
    """
    payload = decode_token(token, expected_type="access")
    return {
        "id":             int(payload["sub"]),
        "email":          payload.get("email"),
        "display_name":   payload.get("display_name"),
        "daily_budget":   float(payload.get("daily_budget",   0.0)),
        "monthly_budget": float(payload.get("monthly_budget", 0.0)),
        "jti":            payload.get("jti"),
    }


# ── OTP helpers ────────────────────────────────────────────────────────────────
# Kept here so auth.py is self-contained for token + OTP primitives.
# The email-sending logic lives in services/otp.py.

import hashlib

def generate_otp(length: int = 6) -> str:
    """Cryptographically secure OTP using secrets module (NOT random)."""
    import string
    return "".join(secrets.choice(string.digits) for _ in range(length))


def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def verify_otp_hash(plain_otp: str, stored_hash: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return secrets.compare_digest(hash_otp(plain_otp), stored_hash)
