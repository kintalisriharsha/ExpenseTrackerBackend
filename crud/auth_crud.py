from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, status
import logging

from auth.auth import verify_otp_hash          # uses constant-time compare
from models.user_model import User, BlacklistedToken, AuthProvider

logger = logging.getLogger(__name__)

OTP_TTL_MINUTES      = 10
OTP_RESEND_COOLDOWN  = 60   # seconds — prevent spam: must wait 60s before re-requesting
OTP_MAX_ATTEMPTS     = 5    # lock OTP after 5 wrong guesses in a window


# ══════════════════════════════════════════════════════════════════════════════
# USER LOOKUPS
# ══════════════════════════════════════════════════════════════════════════════

async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    try:
        result = await db.execute(select(User).where(User.email == email))
        return result.scalars().first()
    except Exception as e:
        logger.error(f"get_user_by_email error: {e}")
        return None


async def get_user_by_google_sub(db: AsyncSession, google_sub: str) -> User | None:
    try:
        result = await db.execute(select(User).where(User.google_sub == google_sub))
        return result.scalars().first()
    except Exception as e:
        logger.error(f"get_user_by_google_sub error: {e}")
        return None


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    try:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalars().first()
    except Exception as e:
        logger.error(f"get_user_by_id error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL + OTP FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def upsert_otp(db: AsyncSession, email: str, hashed_otp: str) -> None:
    """
    Store the hashed OTP on the user row.
    Creates a bare (unverified) user record if one doesn't exist yet.

    Rate-limiting:
        - Raises HTTP 429 if the previous OTP was issued less than
          OTP_RESEND_COOLDOWN seconds ago (prevents email flooding).
    """
    user = await get_user_by_email(db, email)

    if user and user.otp_expires_at:
        # ── Resend cooldown ───────────────────────────────────────────────
        # otp_expires_at is set to now+10min when issued.
        # The OTP is "fresh" if (expires_at - now) > (TTL - cooldown).
        # e.g. expires in 9m50s → issued < 10s ago → block resend.
        now = datetime.now(timezone.utc)
        expires_aware = (
            user.otp_expires_at
            if user.otp_expires_at.tzinfo is not None
            else user.otp_expires_at.replace(tzinfo=timezone.utc)
        )
        seconds_remaining = (expires_aware - now).total_seconds()
        cooldown_threshold = (OTP_TTL_MINUTES * 60) - OTP_RESEND_COOLDOWN

        if seconds_remaining > cooldown_threshold:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Please wait {OTP_RESEND_COOLDOWN} seconds before requesting a new OTP.",
            )

    if not user:
        user = User(
            email          = email,
            auth_provider  = AuthProvider.email,
            email_verified = False,
            otp_attempts   = 0,
        )
        db.add(user)
        await db.flush()

    user.hashed_otp     = hashed_otp
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)
    user.otp_attempts   = 0   # reset attempt counter on new OTP
    await db.flush()


async def verify_and_clear_otp(
    db          : AsyncSession,
    email       : str,
    plain_otp   : str,
    display_name: str | None,
) -> tuple[User, bool]:
    """
    Validate the OTP, clear it, mark email verified.
    Returns (user, is_new_user).
    Raises HTTP 400 / 410 / 429 on bad/expired/too-many OTP.
    """
    user = await get_user_by_email(db, email)
    if not user or not user.hashed_otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No OTP requested for this email",
        )

    # ── Expiry check ──────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    # Fix: always work in UTC-aware datetimes, never use .replace() on an
    # already-aware value.  Use .astimezone() so a naive stamp from DB is
    # still handled safely.
    expires_aware = (
        user.otp_expires_at
        if user.otp_expires_at.tzinfo is not None
        else user.otp_expires_at.replace(tzinfo=timezone.utc)
    )
    if expires_aware < now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="OTP has expired — request a new one",
        )

    # ── Brute-force protection ────────────────────────────────────────────
    attempts = user.otp_attempts or 0
    if attempts >= OTP_MAX_ATTEMPTS:
        # Clear OTP so user must request a fresh one
        user.hashed_otp     = None
        user.otp_expires_at = None
        user.otp_attempts   = 0
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect attempts. Please request a new OTP.",
        )

    # ── Constant-time hash comparison ─────────────────────────────────────
    if not verify_otp_hash(plain_otp, user.hashed_otp):
        user.otp_attempts = attempts + 1
        await db.flush()
        remaining = OTP_MAX_ATTEMPTS - user.otp_attempts
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid OTP. {remaining} attempt(s) remaining.",
        )

    is_new_user = not user.email_verified

    # ── Clear OTP, mark verified ──────────────────────────────────────────
    user.hashed_otp     = None
    user.otp_expires_at = None
    user.otp_attempts   = 0
    user.email_verified = True
    user.updated_at     = now

    if display_name and not user.display_name:
        user.display_name = display_name

    await db.flush()
    return user, is_new_user


# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE SSO FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def get_or_create_google_user(
    db          : AsyncSession,
    google_sub  : str,
    email       : str,
    display_name: str | None,
) -> tuple[User, bool]:
    """
    Find user by google_sub first (most reliable).
    Fall back to email lookup — if found, link Google sub to existing account.
    Returns (user, is_new_user).
    """
    # 1. Look up by Google's stable sub
    user = await get_user_by_google_sub(db, google_sub)
    if user:
        # Keep display_name in sync if Google provides an update
        if display_name and user.display_name != display_name:
            user.display_name = display_name
            await db.flush()
        return user, False

    # 2. Same email already exists — link Google sub to existing account
    user = await get_user_by_email(db, email)
    if user:
        user.google_sub     = google_sub
        user.email_verified = True
        if display_name and not user.display_name:
            user.display_name = display_name
        user.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return user, False

    # 3. Brand new user
    user = User(
        email          = email,
        display_name   = display_name,
        auth_provider  = AuthProvider.google,
        google_sub     = google_sub,
        email_verified = True,
        otp_attempts   = 0,
    )
    db.add(user)
    await db.flush()
    logger.warning(f"New Google user created: id={user.id} email={email}")
    return user, True


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN BLACKLIST
# ══════════════════════════════════════════════════════════════════════════════

async def blacklist_token(
    db        : AsyncSession,
    jti       : str,
    user_id   : int,
    expires_at: datetime,
) -> None:
    try:
        db.add(BlacklistedToken(jti=jti, user_id=user_id, expires_at=expires_at))
        await db.flush()
    except Exception as e:
        logger.error(f"blacklist_token error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to logout",
        )


async def is_token_blacklisted(db: AsyncSession, jti: str) -> bool:
    try:
        result = await db.execute(
            select(BlacklistedToken).where(BlacklistedToken.jti == jti)
        )
        return result.scalars().first() is not None
    except Exception as e:
        logger.error(f"is_token_blacklisted error: {e}")
        return True   # fail closed — treat DB errors as revoked


async def cleanup_expired_blacklist(db: AsyncSession) -> int:
    """
    Delete blacklist rows whose tokens have already expired.
    Call this from a background task or a scheduled job — not on every request.
    Returns the number of rows deleted.
    """
    try:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            delete(BlacklistedToken).where(BlacklistedToken.expires_at < now)
        )
        await db.flush()
        deleted = result.rowcount
        logger.warning(f"Blacklist cleanup: removed {deleted} expired token(s)")
        return deleted
    except Exception as e:
        logger.error(f"cleanup_expired_blacklist error: {e}")
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE UPDATES
# ══════════════════════════════════════════════════════════════════════════════

async def update_user_budgets(
    db             : AsyncSession,
    user_id        : int,
    daily_budget   : float | None,
    monthly_budget : float | None,
) -> User:
    """Update a user's daily/monthly budget targets."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if daily_budget is not None:
        user.daily_budget = daily_budget
    if monthly_budget is not None:
        user.monthly_budget = monthly_budget
    user.updated_at = datetime.now(timezone.utc)

    await db.flush()
    return user


async def update_display_name(
    db          : AsyncSession,
    user_id     : int,
    display_name: str,
) -> User:
    """Update a user's display name."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.display_name = display_name.strip()
    user.updated_at   = datetime.now(timezone.utc)

    await db.flush()
    return user
