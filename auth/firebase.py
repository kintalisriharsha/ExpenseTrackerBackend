import firebase_admin
from firebase_admin import auth, credentials
from dotenv import load_dotenv
from pathlib import Path
from fastapi import HTTPException, status
import logging
import os

logger = logging.getLogger(__name__)

load_dotenv()

# ── Initialize Firebase Admin SDK ──────────────────────────────────────────────
# serviceAccountKey.json is downloaded from:
# Firebase Console → Project Settings → Service Accounts → Generate new private key
# Keep this file in .gitignore — never commit it to GitHub

_SERVICE_ACCOUNT_PATH = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT_PATH",
    "serviceAccountKey.json"   # default — file sitting next to main.py
)

def init_firebase() -> None:
    """
    Initialize Firebase Admin SDK once at app startup.
    Called from main.py lifespan.
    Safe to call multiple times — checks if already initialized.
    """
    if firebase_admin._apps:
        # Already initialized — skip
        return

    key_path = Path(_SERVICE_ACCOUNT_PATH)
    if not key_path.exists():
        raise FileNotFoundError(
            f"Firebase service account key not found at: {key_path.resolve()}\n"
            "Download it from Firebase Console → Project Settings → Service Accounts"
        )

    try:
        cred = credentials.Certificate(str(key_path))
        firebase_admin.initialize_app(cred)
        logger.warning("Firebase Admin SDK initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Firebase Admin SDK: {e}")
        raise


# ── Token Verification ─────────────────────────────────────────────────────────

async def verify_firebase_token(id_token: str) -> dict:
    """
    Verify Firebase ID token sent from Android.

    Returns decoded token containing:
        {
            "uid":          "firebase_uid_string",
            "phone_number": "+91XXXXXXXXXX",
            "exp":          1234567890,
            "iat":          1234567890,
        }

    Raises HTTP 401 if token is invalid or expired.
    Firebase tokens expire after 1 hour —
    Android SDK auto-refreshes them silently.
    """
    try:
        decoded_token = auth.verify_id_token(id_token)

        # Make sure this is a phone auth token
        phone_number = decoded_token.get("phone_number")
        if not phone_number:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token — phone number not found in token"
            )

        return decoded_token

    except auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase token has expired — please re-authenticate"
        )
    except auth.InvalidIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Firebase token"
        )
    except auth.RevokedIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase token has been revoked"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Firebase token verification error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not verify Firebase token"
        )