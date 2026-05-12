from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
import logging

from db import get_db
from auth.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
)
from services.otp import generate_otp, hash_otp, send_otp_email
from auth.Google import verify_google_token
from crud.auth_crud import (
    upsert_otp,
    verify_and_clear_otp,
    get_or_create_google_user,
    get_user_by_id,
    blacklist_token,
    is_token_blacklisted,
)
from schemas.auth_schema import (
    SendOtpRequest,
    VerifyOtpRequest,
    GoogleLoginRequest,
    RefreshTokenRequest,
    LogoutRequest,
    SendOtpResponse,
    LoginResponse,
    RefreshResponse,
    LogoutResponse,
    UserResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── POST /auth/email/send-otp ──────────────────────────────────────────────────

@router.post(
    "/email/send-otp",
    response_model=SendOtpResponse,
    status_code=status.HTTP_200_OK,
)
async def send_otp(
    payload: SendOtpRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 1 of email login.

    - Generates a 6-digit OTP
    - Stores its SHA-256 hash + expiry on the user row (creating a bare
      unverified record if this email has never been seen before)
    - Emails the plain OTP via SMTP

    Android should call this whenever the user submits their email address.
    """
    otp        = generate_otp()
    hashed     = hash_otp(otp)

    await upsert_otp(db, payload.email, hashed)

    try:
        await send_otp_email(payload.email, otp)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to send OTP email — please try again",
        )

    return SendOtpResponse()


# ── POST /auth/email/verify-otp ───────────────────────────────────────────────

@router.post(
    "/email/verify-otp",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
)
async def verify_otp(
    payload: VerifyOtpRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 2 of email login.

    - Validates the OTP (wrong → 400, expired → 410)
    - Clears OTP fields, marks email_verified = True
    - Issues JWT access + refresh tokens

    is_new_user in response:
        true  → first ever successful verify → show onboarding / name setup
        false → returning user               → go straight to home screen

    display_name is optional — Android can send it on the first login
    so the user doesn't need a separate profile-setup step.
    """
    user, is_new_user = await verify_and_clear_otp(
        db,
        payload.email,
        payload.otp,
        payload.display_name,
    )

    access_token  = create_access_token(user)
    refresh_token = create_refresh_token(user)

    logger.warning(
        f"{'New' if is_new_user else 'Returning'} email user: "
        f"id={user.id} email={user.email}"
    )

    return LoginResponse(
        access_token  = access_token,
        refresh_token = refresh_token,
        is_new_user   = is_new_user,
        user          = UserResponse.model_validate(user),
    )


# ── POST /auth/google ─────────────────────────────────────────────────────────

@router.post(
    "/google",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
)
async def google_login(
    payload: GoogleLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Google SSO login (Android One Tap / Sign-In SDK).

    Android flow:
        1. User taps "Sign in with Google"
        2. Google returns an ID token to the app
        3. App posts that token here

    Server flow:
        1. Verify token signature + audience with google-auth library
        2. Upsert user (create if new, link if email already exists)
        3. Return JWT pair

    If the user previously signed up with email+OTP using the same Gmail
    address, their accounts are automatically merged — the Google sub is
    attached to the existing row.
    """
    info = verify_google_token(payload.id_token)

    google_sub   = info.get("sub")
    email        = info.get("email")
    display_name = info.get("name")

    if not google_sub or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google token missing sub or email",
        )

    if not info.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google email is not verified",
        )

    user, is_new_user = await get_or_create_google_user(
        db, google_sub, email, display_name
    )

    access_token  = create_access_token(user)
    refresh_token = create_refresh_token(user)

    logger.warning(
        f"Google login: id={user.id} email={user.email} new={is_new_user}"
    )

    return LoginResponse(
        access_token  = access_token,
        refresh_token = refresh_token,
        is_new_user   = is_new_user,
        user          = UserResponse.model_validate(user),
    )


# ── POST /auth/refresh ────────────────────────────────────────────────────────

@router.post(
    "/refresh",
    response_model=RefreshResponse,
    status_code=status.HTTP_200_OK,
)
async def refresh_token(
    payload: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange a valid refresh token for a new access token.
    Returns 401 if the refresh token has been blacklisted (logged out).
    """
    token_payload = decode_token(payload.refresh_token, expected_type="refresh")
    user_id       = int(token_payload["sub"])
    jti           = token_payload.get("jti")

    if not jti:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid refresh token — missing jti")

    if await is_token_blacklisted(db, jti):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token revoked — please log in again")

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User not found")

    return RefreshResponse(access_token=create_access_token(user))


# ── DELETE /auth/logout ───────────────────────────────────────────────────────

@router.delete(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
)
async def logout(
    payload     : LogoutRequest,
    db          : AsyncSession = Depends(get_db),
    current_user: dict         = Depends(get_current_user),
):
    """
    Invalidate the refresh token server-side.
    The access token expires on its own within 15 minutes.
    """
    try:
        token_payload = decode_token(payload.refresh_token, expected_type="refresh")
    except Exception:
        return LogoutResponse()   # already expired — effectively logged out

    jti        = token_payload.get("jti")
    expires_at = datetime.fromtimestamp(token_payload["exp"], tz=timezone.utc)

    if not jti:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid refresh token — missing jti")

    await blacklist_token(db, jti, current_user["id"], expires_at)
    logger.warning(f"User logged out: id={current_user['id']}")
    return LogoutResponse()


# ── GET /auth/me ──────────────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
)
async def get_me(
    db          : AsyncSession = Depends(get_db),
    current_user: dict         = Depends(get_current_user),
):
    """Return the authenticated user's profile."""
    user = await get_user_by_id(db, current_user["id"])
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="User not found")
    return UserResponse.model_validate(user)
