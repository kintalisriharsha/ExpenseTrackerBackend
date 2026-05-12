from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, status
import logging
from services.otp import verify_otp_hash   # local import avoids circular

from models.user_model import User, BlacklistedToken, AuthProvider

logger = logging.getLogger(__name__)

OTP_TTL_MINUTES = 10


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
    Creates a bare (unverified) user record if one doesn't exist yet —
    full profile is filled in on successful OTP verify.
    """
    user = await get_user_by_email(db, email)
    if not user:
        user = User(
            email         = email,
            auth_provider = AuthProvider.email,
            email_verified= False,
        )
        db.add(user)
        await db.flush()   # get id without committing

    user.hashed_otp     = hashed_otp
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)
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
    Raises HTTP 400/410 on bad/expired OTP.
    """
    
    user = await get_user_by_email(db, email)
    if not user or not user.hashed_otp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No OTP requested for this email")

    now = datetime.now(timezone.utc)
    if user.otp_expires_at and user.otp_expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=status.HTTP_410_GONE,
                            detail="OTP has expired — request a new one")

    if not verify_otp_hash(plain_otp, user.hashed_otp):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid OTP")

    is_new_user = not user.email_verified   # first successful verify = new user

    # Clear OTP, mark verified, optionally set name
    user.hashed_otp     = None
    user.otp_expires_at = None
    user.email_verified = True
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
    Fall back to email lookup in case they previously signed up with email OTP
    — if so, link their Google sub to the existing account.
    Returns (user, is_new_user).
    """
    # 1. Look up by Google's stable sub
    user = await get_user_by_google_sub(db, google_sub)
    if user:
        return user, False

    # 2. Same email already exists (was an email+OTP user) — link Google sub
    user = await get_user_by_email(db, email)
    if user:
        user.google_sub    = google_sub
        user.email_verified = True       # Google guarantees email_verified
        if display_name and not user.display_name:
            user.display_name = display_name
        await db.flush()
        return user, False

    # 3. Brand new user
    user = User(
        email          = email,
        display_name   = display_name,
        auth_provider  = AuthProvider.google,
        google_sub     = google_sub,
        email_verified = True,
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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to logout")


async def is_token_blacklisted(db: AsyncSession, jti: str) -> bool:
    try:
        result = await db.execute(
            select(BlacklistedToken).where(BlacklistedToken.jti == jti)
        )
        return result.scalars().first() is not None
    except Exception as e:    
        logger.error(f"is_token_blacklisted error: {e}")
        return True   # fail closed
