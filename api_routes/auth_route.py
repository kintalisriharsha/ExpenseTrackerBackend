from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
import logging

from db import get_db
from auth.firebase import verify_firebase_token
from auth.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
)
from crud.auth_crud import (
    get_or_create_user,
    get_user_by_id,
    create_user,
    blacklist_token,
    is_token_blacklisted,
)
from schemas.auth_schema import (
    FirebaseLoginRequest,
    RegisterRequest,
    RefreshTokenRequest,
    LogoutRequest,
    LoginResponse,
    RegisterResponse,
    RefreshResponse,
    LogoutResponse,
    UserResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── POST /auth/login ───────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
async def login(
    payload: FirebaseLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Login or Register with Firebase phone authentication.

    Android flow:
        1. User enters phone → Firebase sends OTP SMS
        2. User enters OTP  → Firebase verifies it
        3. Firebase returns id_token to Android
        4. Android sends id_token here

    This endpoint:
        1. Verifies id_token with Firebase Admin SDK
        2. Creates user in Neon DB if first login (register)
        3. Fetches user if returning (login)
        4. Returns JWT access + refresh tokens

    is_new_user in response:
        true  → show onboarding / profile setup screen
        false → go straight to home screen
    """
    decoded = await verify_firebase_token(payload.id_token)

    firebase_uid = decoded.get("uid")
    phone_number = decoded.get("phone_number")

    if not firebase_uid or not phone_number:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Firebase token — missing uid or phone_number"
        )

    user, is_new_user = await get_or_create_user(db, firebase_uid, phone_number)

    access_token  = create_access_token(user)
    refresh_token = create_refresh_token(user)

    logger.warning(
        f"{'New user registered' if is_new_user else 'User logged in'}: "
        f"id={user.id} phone={user.phone_number}"
    )

    return LoginResponse(
        access_token  = access_token,
        refresh_token = refresh_token,
        token_type    = "bearer",
        is_new_user   = is_new_user,
        user          = UserResponse.model_validate(user),
    )


# ── POST /auth/register ────────────────────────────────────────────────────────

@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new user with full details after OTP verification.

    Android signup flow:
        1. SignUpScreen collects: display_name, email, phone, password
        2. OtpVerifyScreen sends OTP → Firebase verifies
        3. Firebase returns id_token
        4. Android calls this endpoint with id_token + the 4 fields

    The 4 fields:
        id_token       → proves phone ownership via Firebase
        display_name   → full name from SignUpScreen
        daily_budget   → from budget setup (0.0 if skipped)
        monthly_budget → from budget setup (0.0 if skipped)

    Note: email and password from SignUpScreen are handled by Firebase Auth,
    not stored in your Neon DB. Only phone is stored — it comes from the
    decoded Firebase token, not from the request body, so it can't be spoofed.
    """
    decoded = await verify_firebase_token(payload.id_token)

    firebase_uid = decoded.get("uid")
    phone_number = decoded.get("phone_number")

    if not firebase_uid or not phone_number:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Firebase token — missing uid or phone_number"
        )

    user = await create_user(
        db             = db,
        firebase_uid   = firebase_uid,
        phone_number   = phone_number,
        display_name   = payload.display_name,
        daily_budget   = payload.daily_budget,
        monthly_budget = payload.monthly_budget,
    )

    access_token  = create_access_token(user)
    refresh_token = create_refresh_token(user)

    logger.warning(f"User registered via /register: id={user.id} phone={user.phone_number}")

    return RegisterResponse(
        access_token  = access_token,
        refresh_token = refresh_token,
        token_type    = "bearer",
        user          = UserResponse.model_validate(user),
    )


# ── POST /auth/refresh ─────────────────────────────────────────────────────────

@router.post("/refresh", response_model=RefreshResponse, status_code=status.HTTP_200_OK)
async def refresh_token(
    payload: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Get a new access_token using the refresh_token.

    Android calls this when:
        - API returns 401 Unauthorized
        - access_token is about to expire (every ~15 min)

    Now checks blacklist first — if user has logged out, this returns 401.
    """
    token_payload = decode_token(payload.refresh_token, expected_type="refresh")
    user_id       = int(token_payload.get("sub"))
    jti           = token_payload.get("jti")

    # ── Blacklist check ────────────────────────────────────────────────────────
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token — missing jti"
        )

    if await is_token_blacklisted(db, jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked — please log in again"
        )

    # ── Fetch latest user — budget may have changed since last login ───────────
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    new_access_token = create_access_token(user)

    return RefreshResponse(
        access_token = new_access_token,
        token_type   = "bearer",
    )


# ── DELETE /auth/logout ────────────────────────────────────────────────────────

@router.delete("/logout", response_model=LogoutResponse, status_code=status.HTTP_200_OK)
async def logout(
    payload      : LogoutRequest,
    db           : AsyncSession = Depends(get_db),
    current_user : dict        = Depends(get_current_user),
):
    """
    Logout — invalidate the refresh token server-side.

    Android flow:
        1. Call DELETE /auth/logout with refresh_token in body
        2. Delete access_token + refresh_token from EncryptedSharedPreferences
        3. Navigate to login screen

    Server flow:
        1. Verify the refresh token is valid (not already expired)
        2. Extract jti from the token
        3. Store jti in blacklisted_tokens table
        4. /auth/refresh will now reject this token forever

    Access token note:
        We don't blacklist the access token — it expires in 15 min anyway.
        Only the refresh token can get new access tokens, so blacklisting it
        is sufficient for true server-side logout.
    """
    try:
        token_payload = decode_token(payload.refresh_token, expected_type="refresh")
    except Exception:
        # Token already expired — logout is effectively done
        return LogoutResponse(detail="Successfully logged out")

    jti        = token_payload.get("jti")
    expires_at = datetime.fromtimestamp(token_payload.get("exp"), tz=timezone.utc)

    if not jti:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid refresh token — missing jti"
        )

    await blacklist_token(
        db         = db,
        jti        = jti,
        user_id    = current_user["id"],
        expires_at = expires_at,
    )

    logger.warning(f"User logged out: id={current_user['id']}")

    return LogoutResponse(detail="Successfully logged out")


# ── GET /auth/me ───────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def get_me(
    db           : AsyncSession = Depends(get_db),
    current_user : dict        = Depends(get_current_user),
):
    """
    Returns the currently authenticated user's profile.
    Used by Profile screen to load user details.
    Requires valid access_token in Authorization header.
    """
    user = await get_user_by_id(db, current_user["id"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return UserResponse.model_validate(user)