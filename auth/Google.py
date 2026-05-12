from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fastapi import HTTPException, status
import os, logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
if not GOOGLE_CLIENT_ID:
    raise ValueError("GOOGLE_CLIENT_ID is not set in .env")


def verify_google_token(token: str) -> dict:
    """
    Verify a Google ID token issued by the Android Sign-In SDK.

    Returns dict with at minimum:
        sub   → Google's unique user ID (stable across sessions)
        email → user's Google email
        name  → display name (may be absent for some accounts)
        email_verified → bool

    Raises HTTP 401 on any failure.
    """
    try:
        info = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
        # Double-check audience in case token was issued for a different client
        if info.get("aud") != GOOGLE_CLIENT_ID:
            raise ValueError("Token audience mismatch")

        return info

    except ValueError as e:
        logger.warning(f"Google token verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid Google token: {e}",
        )
    except Exception as e:
        logger.error(f"Unexpected error verifying Google token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not verify Google token",
        )
