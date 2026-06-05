# from fastapi import APIRouter, Depends, HTTPException, status
# from sqlalchemy.ext.asyncio import AsyncSession
# from datetime import datetime, timezone
# import logging

# from db import get_db
# from auth.auth import (
#     create_access_token,
#     create_refresh_token,
#     decode_token,
#     get_current_user,
# )
# from services.otp import generate_otp, hash_otp_async   # ← async hash
# from services.otpEmail  import send_otp_email                 # ← email sender
# from auth.Google import verify_google_token
# from crud.auth_crud import (
#     upsert_otp,
#     verify_and_clear_otp,
#     get_or_create_google_user,
#     get_user_by_id,
#     blacklist_token,
#     is_token_blacklisted,
#     cleanup_expired_blacklist,
#     update_user_profile,
#     update_user_budgets,
# )
# from schemas.auth_schema import (
#     SendOtpRequest,
#     VerifyOtpRequest,
#     GoogleLoginRequest,
#     RefreshTokenRequest,
#     LogoutRequest,
#     UpdateProfileRequest,
#     UpdateBudgetRequest,
#     SendOtpResponse,
#     LoginResponse,
#     RefreshResponse,
#     LogoutResponse,
#     UserResponse,
#     MessageResponse,
# )

# # from cache import cache_delete, home_key, settings_key

# logger = logging.getLogger(__name__)

# router = APIRouter(prefix="/auth", tags=["auth"])


# # ── POST /auth/email/send-otp ──────────────────────────────────────────────────

# @router.post(
#     "/email/send-otp",
#     response_model=SendOtpResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Request OTP — works for both login and registration",
# )
# async def send_otp(
#     payload: SendOtpRequest,
#     db: AsyncSession = Depends(get_db),
# ):
#     """
#     Generates a 6-digit OTP and emails it.
#     Rate-limited: can't re-request within 60 seconds of the previous OTP.
#     Creates a bare user row if the email has never been seen before.
#     """
#     otp    = generate_otp()
#     hashed = await hash_otp_async(otp)   # ← non-blocking; offloaded to thread pool

#     await upsert_otp(db, payload.email, hashed)

#     try:
#         await send_otp_email(payload.email, otp)
#     except Exception:
#         raise HTTPException(
#             status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
#             detail="Failed to send OTP email — please try again",
#         )

#     return SendOtpResponse()


# # ── POST /auth/email/verify-otp ───────────────────────────────────────────────

# @router.post(
#     "/email/verify-otp",
#     response_model=LoginResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Verify OTP — handles both login and registration",
# )
# async def verify_otp(
#     payload: VerifyOtpRequest,
#     db: AsyncSession = Depends(get_db),
# ):
#     """
#     Validates the OTP.

#     Errors:
#         400 – wrong OTP (with remaining attempt count)
#         410 – OTP expired
#         429 – too many wrong attempts

#     Response fields:
#         is_new_user = true  → first ever login → Android navigates to CreateAccount screen
#         is_new_user = false → returning user   → Android navigates to Home screen
#     """
#     user, is_new_user = await verify_and_clear_otp(
#         db,
#         payload.email,
#         payload.otp,
#         payload.display_name,
#         payload.mobile_number,
#     )

#     access_token  = create_access_token(user)
#     refresh_token = create_refresh_token(user)

#     logger.warning(
#         f"{'New' if is_new_user else 'Returning'} email user: "
#         f"id={user.id} email={user.email}"
#     )

#     return LoginResponse(
#         access_token  = access_token,
#         refresh_token = refresh_token,
#         is_new_user   = is_new_user,
#         user          = UserResponse.model_validate(user),
#     )


# # ── POST /auth/google ─────────────────────────────────────────────────────────

# @router.post(
#     "/google",
#     response_model=LoginResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Google SSO login / registration",
# )
# async def google_login(
#     payload: GoogleLoginRequest,
#     db: AsyncSession = Depends(get_db),
# ):
#     """
#     Verifies a Google ID token from the Android Sign-In SDK.
#     Creates or links the user account, returns JWT pair.
#     is_new_user=true → navigate to CreateAccount to fill mobile number.
#     """
#     info = verify_google_token(payload.id_token)

#     google_sub   = info.get("sub")
#     email        = info.get("email")
#     display_name = info.get("name")

#     if not google_sub or not email:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Google token missing sub or email",
#         )

#     if not info.get("email_verified", False):
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Google email is not verified",
#         )

#     user, is_new_user = await get_or_create_google_user(
#         db, google_sub, email, display_name
#     )

#     access_token  = create_access_token(user)
#     refresh_token = create_refresh_token(user)

#     logger.warning(
#         f"Google login: id={user.id} email={user.email} new={is_new_user}"
#     )

#     return LoginResponse(
#         access_token  = access_token,
#         refresh_token = refresh_token,
#         is_new_user   = is_new_user,
#         user          = UserResponse.model_validate(user),
#     )


# # ── POST /auth/refresh ────────────────────────────────────────────────────────

# @router.post(
#     "/refresh",
#     response_model=RefreshResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Exchange refresh token for a new access token",
# )
# async def refresh_token(
#     payload: RefreshTokenRequest,
#     db: AsyncSession = Depends(get_db),
# ):
#     token_payload = decode_token(payload.refresh_token, expected_type="refresh")
#     user_id       = int(token_payload["sub"])
#     jti           = token_payload.get("jti")

#     if not jti:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Invalid refresh token — missing jti",
#         )

#     if await is_token_blacklisted(db, jti):
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Token revoked — please log in again",
#         )

#     user = await get_user_by_id(db, user_id)
#     if not user:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="User not found",
#         )

#     return RefreshResponse(access_token=create_access_token(user))


# # ── POST /auth/logout ─────────────────────────────────────────────────────────

# @router.post(
#     "/logout",
#     response_model=LogoutResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Invalidate refresh token (logout)",
# )
# async def logout(
#     payload     : LogoutRequest,
#     db          : AsyncSession = Depends(get_db),
#     current_user: dict         = Depends(get_current_user),
# ):
#     """
#     Blacklists the refresh token. Access token expires naturally (15 min).
#     Idempotent — sending an already-expired refresh token still returns 200.
#     """
#     try:
#         token_payload = decode_token(payload.refresh_token, expected_type="refresh")
#     except HTTPException:
#         return LogoutResponse()   # already expired → effectively logged out

#     jti        = token_payload.get("jti")
#     expires_at = datetime.fromtimestamp(token_payload["exp"], tz=timezone.utc)

#     if not jti:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Invalid refresh token — missing jti",
#         )

#     await blacklist_token(db, jti, current_user["id"], expires_at)
#     # await cache_delete(home_key(current_user["id"]), settings_key(current_user["id"]))
#     logger.warning(f"User logged out: id={current_user['id']}")
#     return LogoutResponse()


# # ── GET /auth/me ──────────────────────────────────────────────────────────────

# @router.get("/me", response_model=UserResponse)
# async def get_my_profile(
#     db: AsyncSession = Depends(get_db),
#     current_user: dict = Depends(get_current_user) # Get ID from token
# ):
#     # Fetch FRESH data from DB using the ID
#     user = await get_user_by_id(db, current_user["id"])
#     if not user:
#         raise HTTPException(status_code=404, detail="User not found")
#     return user # This returns the fresh DB values (5000.0)


# # ── PATCH /auth/me/profile ────────────────────────────────────────────────────

# @router.patch(
#     "/me/profile",
#     response_model=UserResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Update display name and/or mobile number",
# )
# async def update_profile(
#     payload     : UpdateProfileRequest,
#     db          : AsyncSession = Depends(get_db),
#     current_user: dict         = Depends(get_current_user),
# ):
#     user = await update_user_profile(
#         db,
#         current_user["id"],
#         payload.display_name,
#         payload.mobile_number,
#     )
#     return UserResponse.model_validate(user)


# # ── PATCH /auth/me/budget ─────────────────────────────────────────────────────

# @router.patch(
#     "/me/budget",
#     response_model=UserResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Update daily / monthly budget targets",
# )
# async def update_budget(
#     payload     : UpdateBudgetRequest,
#     db          : AsyncSession = Depends(get_db),
#     current_user: dict         = Depends(get_current_user),
# ):
#     user = await update_user_budgets(
#         db,
#         current_user["id"],
#         payload.daily_budget,
#         payload.monthly_budget,
#     )
#     return UserResponse.model_validate(user)

"""
auth_route.py
─────────────
FastAPI auth router with Redis-backed caching and rate limiting.
No circuit breaker — plain try/except in cache.py handles all Redis failures.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
import logging

from db import get_db
from auth.auth import create_access_token, create_refresh_token, decode_token, get_current_user
from services.otp import generate_otp, hash_otp_async
from services.otpEmail import send_otp_email
from auth.Google import verify_google_token
from crud.auth_crud import (
    upsert_otp, verify_and_clear_otp, get_or_create_google_user,
    get_user_by_id, blacklist_token, is_token_blacklisted,
    cleanup_expired_blacklist, update_user_profile, update_user_budgets,
)
from schemas.auth_schema import (
    SendOtpRequest, VerifyOtpRequest, GoogleLoginRequest,
    RefreshTokenRequest, LogoutRequest, UpdateProfileRequest, UpdateBudgetRequest,
    SendOtpResponse, LoginResponse, RefreshResponse, LogoutResponse,
    UserResponse, MessageResponse,
)
from cache import (
    cache_get, cache_set, cache_delete, cache_delete_user,
    me_key, home_key, rate_limit_login_check, rate_limit_login_reset,
    TTL_ME,
)
from fastapi.encoders import jsonable_encoder

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── POST /auth/email/send-otp ──────────────────────────────────────────────────

@router.post("/email/send-otp", response_model=SendOtpResponse,
             status_code=status.HTTP_200_OK,
             summary="Request OTP — works for both login and registration")
async def send_otp(
    payload : SendOtpRequest,
    request : Request,
    db      : AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)

    # IP rate limit — (False, 0) if Redis is down, never blocks
    blocked, _ = await rate_limit_login_check(ip, max_attempts=10)
    if blocked:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many OTP requests — please try again later.")

    otp    = generate_otp()
    hashed = await hash_otp_async(otp)
    await upsert_otp(db, payload.email, hashed)

    try:
        await send_otp_email(payload.email, otp)
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Failed to send OTP email — please try again")

    return SendOtpResponse()


# ── POST /auth/email/verify-otp ───────────────────────────────────────────────

@router.post("/email/verify-otp", response_model=LoginResponse,
             status_code=status.HTTP_200_OK,
             summary="Verify OTP — handles both login and registration")
async def verify_otp(
    payload : VerifyOtpRequest,
    request : Request,
    db      : AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)

    user, is_new_user = await verify_and_clear_otp(
        db, payload.email, payload.otp, payload.display_name, payload.mobile_number,
    )

    await rate_limit_login_reset(ip)           # reset IP counter on success
    await cache_delete(me_key(user.id))        # bust stale profile cache

    access_token  = create_access_token(user)
    refresh_token = create_refresh_token(user)

    logger.warning(f"{'New' if is_new_user else 'Returning'} email user: id={user.id} email={user.email}")

    return LoginResponse(
        access_token=access_token, refresh_token=refresh_token,
        is_new_user=is_new_user, user=UserResponse.model_validate(user),
    )


# ── POST /auth/google ─────────────────────────────────────────────────────────

@router.post("/google", response_model=LoginResponse,
             status_code=status.HTTP_200_OK,
             summary="Google SSO login / registration")
async def google_login(
    payload : GoogleLoginRequest,
    request : Request,
    db      : AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)

    blocked, _ = await rate_limit_login_check(ip, max_attempts=10)
    if blocked:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many login attempts — please try again later.")

    info = verify_google_token(payload.id_token)
    google_sub   = info.get("sub")
    email        = info.get("email")
    display_name = info.get("name")

    if not google_sub or not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Google token missing sub or email")

    if not info.get("email_verified", False):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Google email is not verified")

    user, is_new_user = await get_or_create_google_user(db, google_sub, email, display_name)

    await rate_limit_login_reset(ip)
    await cache_delete(me_key(user.id))

    access_token  = create_access_token(user)
    refresh_token = create_refresh_token(user)
    logger.warning(f"Google login: id={user.id} email={user.email} new={is_new_user}")

    return LoginResponse(
        access_token=access_token, refresh_token=refresh_token,
        is_new_user=is_new_user, user=UserResponse.model_validate(user),
    )


# ── POST /auth/refresh ────────────────────────────────────────────────────────

@router.post("/refresh", response_model=RefreshResponse,
             status_code=status.HTTP_200_OK,
             summary="Exchange refresh token for a new access token")
async def refresh_token(
    payload : RefreshTokenRequest,
    db      : AsyncSession = Depends(get_db),
):
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


# ── POST /auth/logout ─────────────────────────────────────────────────────────

@router.post("/logout", response_model=LogoutResponse,
             status_code=status.HTTP_200_OK,
             summary="Invalidate refresh token (logout)")
async def logout(
    payload     : LogoutRequest,
    db          : AsyncSession = Depends(get_db),
    current_user: dict         = Depends(get_current_user),
):
    try:
        token_payload = decode_token(payload.refresh_token, expected_type="refresh")
    except HTTPException:
        return LogoutResponse()

    jti        = token_payload.get("jti")
    expires_at = datetime.fromtimestamp(token_payload["exp"], tz=timezone.utc)

    if not jti:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid refresh token — missing jti")

    await blacklist_token(db, jti, current_user["id"], expires_at)
    await cache_delete_user(current_user["id"])   # wipe all user caches

    logger.warning(f"User logged out: id={current_user['id']}")
    return LogoutResponse()


# ── GET /auth/me (cached) ─────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def get_my_profile(
    db          : AsyncSession = Depends(get_db),
    current_user: dict         = Depends(get_current_user),
):
    user_id = current_user["id"]
    key     = me_key(user_id)

    cached = await cache_get(key)
    if cached:
        return cached

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    response = UserResponse.model_validate(user)
    await cache_set(key, jsonable_encoder(response), TTL_ME)
    return response


# ── PATCH /auth/me/profile ────────────────────────────────────────────────────

@router.patch("/me/profile", response_model=UserResponse,
              status_code=status.HTTP_200_OK,
              summary="Update display name and/or mobile number")
async def update_profile(
    payload     : UpdateProfileRequest,
    db          : AsyncSession = Depends(get_db),
    current_user: dict         = Depends(get_current_user),
):
    user = await update_user_profile(
        db, current_user["id"], payload.display_name, payload.mobile_number,
    )
    await cache_delete(me_key(current_user["id"]))
    return UserResponse.model_validate(user)


# ── PATCH /auth/me/budget ─────────────────────────────────────────────────────

@router.patch("/me/budget", response_model=UserResponse,
              status_code=status.HTTP_200_OK,
              summary="Update daily / monthly budget targets")
async def update_budget(
    payload     : UpdateBudgetRequest,
    db          : AsyncSession = Depends(get_db),
    current_user: dict         = Depends(get_current_user),
):
    user = await update_user_budgets(
        db, current_user["id"], payload.daily_budget, payload.monthly_budget,
    )
    await cache_delete(
        me_key(current_user["id"]),
        home_key(current_user["id"]),
    )
    return UserResponse.model_validate(user)