from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, status
import logging, secrets, hashlib

from models.user_model import User, BlacklistedToken, AuthProvider

logger = logging.getLogger(__name__)

OTP_TTL_MINUTES     = 10
OTP_RESEND_COOLDOWN = 60   # seconds — must wait before requesting another OTP
OTP_MAX_ATTEMPTS    = 5    # wrong guesses before the OTP is invalidated


# ── Internal helpers ──────────────────────────────────────────────────────────

def _to_utc(dt: datetime) -> datetime:
    """Always return a UTC-aware datetime, whether the input is naive or aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _verify_otp_hash(plain: str, stored_hash: str) -> bool:
    candidate = hashlib.sha256(plain.encode()).hexdigest()
    return secrets.compare_digest(candidate, stored_hash)


async def _init_default_settings(db: AsyncSession, user_id: int) -> None:
    """
    Create a Settings row with default values for a newly registered user.
    Calls init_budget from setting_crud directly — same path the route uses,
    so all seeding logic (current month, zero values) lives in one place.
    Imported locally to avoid a circular import between auth_crud ↔ setting_crud.
    """
    from crud.setting_crud import init_budget
    from schemas.setting_schema import BudgetInit

    payload = BudgetInit(
        monthly_budget       = 0.0,
        daily_limit          = 0.0,
        notification_enabled = False,
        is_dark_mode         = False,
        apply_to_all_months  = False,   # only seed the current month
    )

    try:
        await init_budget(db, user_id, payload)
        logger.warning(f"Default settings created for user_id={user_id}")
    except HTTPException as e:
        # 409 = settings already exist — safe to swallow, never block signup
        if e.status_code == status.HTTP_409_CONFLICT:
            logger.warning(f"Settings already exist for user_id={user_id} — skipping init")
        else:
            raise


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
    Creates a bare unverified record if the email is new.

    Rate-limiting:
        Raises HTTP 429 if a valid OTP was issued in the last OTP_RESEND_COOLDOWN seconds.

    Note: Settings are NOT initialised here — the user hasn't verified ownership
    of the email yet. Init happens in verify_and_clear_otp on first success.
    """
    now  = datetime.now(timezone.utc)
    user = await get_user_by_email(db, email)

    if user and user.otp_expires_at:
        expires_utc       = _to_utc(user.otp_expires_at)
        seconds_remaining = (expires_utc - now).total_seconds()
        cooldown_threshold = (OTP_TTL_MINUTES * 60) - OTP_RESEND_COOLDOWN

        if seconds_remaining > cooldown_threshold:
            wait = int(OTP_RESEND_COOLDOWN - ((OTP_TTL_MINUTES * 60) - seconds_remaining))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Please wait {max(wait, 1)} second(s) before requesting a new OTP.",
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
    user.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    user.otp_attempts   = 0
    user.updated_at     = now
    await db.flush()


async def verify_and_clear_otp(
    db            : AsyncSession,
    email         : str,
    plain_otp     : str,
    display_name  : str | None,
    mobile_number : str | None,
) -> tuple[User, bool]:
    """
    Validate OTP → clear it → mark email verified.

    Returns (user, is_new_user).
    Raises:
        400 – no OTP on record / wrong code
        410 – OTP expired
        429 – too many wrong attempts

    On first successful verify (is_new_user=True), default Settings are
    initialised automatically so the app never needs to call /budget/init.
    """
    user = await get_user_by_email(db, email)
    if not user or not user.hashed_otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No OTP requested for this email",
        )

    now = datetime.now(timezone.utc)

    # Expiry check — always compare UTC-aware datetimes
    if _to_utc(user.otp_expires_at) < now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="OTP has expired — request a new one",
        )

    # Brute-force check
    attempts = user.otp_attempts or 0
    if attempts >= OTP_MAX_ATTEMPTS:
        user.hashed_otp     = None
        user.otp_expires_at = None
        user.otp_attempts   = 0
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect attempts — please request a new OTP.",
        )

    # Constant-time comparison
    if not _verify_otp_hash(plain_otp, user.hashed_otp):
        user.otp_attempts = attempts + 1
        await db.flush()
        remaining = OTP_MAX_ATTEMPTS - user.otp_attempts
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid OTP — {remaining} attempt(s) remaining.",
        )

    is_new_user = not user.email_verified

    # Clear OTP, mark verified
    user.hashed_otp     = None
    user.otp_expires_at = None
    user.otp_attempts   = 0
    user.email_verified = True
    user.updated_at     = now

    # Profile fields only set if provided and not already set
    if display_name and not user.display_name:
        user.display_name = display_name.strip()
    if mobile_number and not user.mobile_number:
        user.mobile_number = mobile_number.strip()

    await db.flush()

    # Initialise default settings for brand-new users only
    if is_new_user:
        await _init_default_settings(db, user.id)

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
    Look up by google_sub first, then by email (merge existing OTP account),
    else create brand new.
    Returns (user, is_new_user).

    On brand-new Google sign-in (is_new_user=True), default Settings are
    initialised automatically.
    """
    now = datetime.now(timezone.utc)

    # 1. Known Google user — returning, no init needed
    user = await get_user_by_google_sub(db, google_sub)
    if user:
        if display_name and user.display_name != display_name:
            user.display_name = display_name
            user.updated_at   = now
            await db.flush()
        return user, False

    # 2. Email already registered via OTP — link Google sub, no init needed
    user = await get_user_by_email(db, email)
    if user:
        user.google_sub     = google_sub
        user.email_verified = True
        user.updated_at     = now
        if display_name and not user.display_name:
            user.display_name = display_name
        await db.flush()
        return user, False

    # 3. Brand new Google user — create then init default settings
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

    await _init_default_settings(db, user.id)

    return user, True


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE UPDATES
# ══════════════════════════════════════════════════════════════════════════════

async def update_user_profile(
    db           : AsyncSession,
    user_id      : int,
    display_name : str | None,
    mobile_number: str | None,
) -> User:
    """Update display name and/or mobile number. Only changes non-None fields."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if display_name is not None:
        user.display_name = display_name.strip()
    if mobile_number is not None:
        user.mobile_number = mobile_number.strip()
    user.updated_at = datetime.now(timezone.utc)

    await db.flush()
    return user


async def update_user_budgets(
    db            : AsyncSession,
    user_id       : int,
    daily_budget  : float | None,
    monthly_budget: float | None,
) -> User:
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
        return True   # fail closed


async def cleanup_expired_blacklist(db: AsyncSession) -> int:
    """Delete rows whose tokens have already expired. Safe to call any time."""
    try:
        result = await db.execute(
            delete(BlacklistedToken).where(
                BlacklistedToken.expires_at < datetime.now(timezone.utc)
            )
        )
        await db.flush()
        return result.rowcount
    except Exception as e:
        logger.error(f"cleanup_expired_blacklist error: {e}")
        return 0