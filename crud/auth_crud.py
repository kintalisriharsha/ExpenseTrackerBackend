from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
from fastapi import HTTPException, status
import logging

from models.user_model import User
from models.user_model import BlacklistedToken

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# USER CRUD
# ══════════════════════════════════════════════════════════════════════════════

async def get_user_by_firebase_uid(
    db: AsyncSession,
    firebase_uid: str
) -> User | None:
    """
    Fetch user by Firebase UID.
    Uses idx_users_firebase_uid index — fast lookup on every login.
    """
    try:
        stmt   = select(User).where(User.firebase_uid == firebase_uid)
        result = await db.execute(stmt)
        return result.scalars().first()
    except Exception as e:
        logger.error(f"Error fetching user by firebase_uid: {e}")
        return None


async def get_user_by_id(
    db: AsyncSession,
    user_id: int
) -> User | None:
    """
    Fetch user by Neon DB id.
    Used by GET /auth/me and POST /auth/refresh.
    """
    try:
        stmt   = select(User).where(User.id == user_id)
        result = await db.execute(stmt)
        return result.scalars().first()
    except Exception as e:
        logger.error(f"Error fetching user by id: {e}")
        return None


async def create_user(
    db           : AsyncSession,
    firebase_uid : str,
    phone_number : str,
    display_name : str | None = None,
    daily_budget : float = 0.0,
    monthly_budget: float = 0.0,
) -> User:
    """
    Create a new user in Neon DB.

    Called by:
        - POST /auth/register  → all 4 fields provided upfront
        - get_or_create_user   → only firebase_uid + phone (login flow)

    display_name, daily_budget, monthly_budget default to None/0 if not provided
    so the login flow still works without them.
    """
    try:
        # Race condition guard — check again before insert
        existing = await get_user_by_firebase_uid(db, firebase_uid)
        if existing:
            return existing

        user = User(
            firebase_uid   = firebase_uid,
            phone_number   = phone_number,
            display_name   = display_name,
            daily_budget   = daily_budget,
            monthly_budget = monthly_budget,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)

        logger.warning(f"New user created: id={user.id} phone={user.phone_number}")
        return user

    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user"
        )


async def get_or_create_user(
    db           : AsyncSession,
    firebase_uid : str,
    phone_number : str,
) -> tuple[User, bool]:
    """
    Login or Register in one call (used by /auth/login Firebase flow).
    Returns (user, is_new_user).
        is_new_user=True  → first time login → Android shows onboarding
        is_new_user=False → returning user   → go straight to home screen
    """
    user = await get_user_by_firebase_uid(db, firebase_uid)
    if user:
        return user, False

    user = await create_user(db, firebase_uid, phone_number)
    return user, True


# ══════════════════════════════════════════════════════════════════════════════
# BLACKLIST CRUD
# ══════════════════════════════════════════════════════════════════════════════

async def blacklist_token(
    db         : AsyncSession,
    jti        : str,
    user_id    : int,
    expires_at : datetime,
) -> None:
    """
    Add a refresh token's jti to the blacklist.
    Called by DELETE /auth/logout.

    We store expires_at so a cleanup job can delete expired rows later
    with: DELETE FROM blacklisted_tokens WHERE expires_at < NOW()
    """
    try:
        entry = BlacklistedToken(
            jti        = jti,
            user_id    = user_id,
            expires_at = expires_at,
        )
        db.add(entry)
        await db.flush()
        logger.info(f"Token blacklisted: jti={jti} user_id={user_id}")
    except Exception as e:
        logger.error(f"Error blacklisting token: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to logout"
        )


async def is_token_blacklisted(
    db  : AsyncSession,
    jti : str,
) -> bool:
    """
    Check if a refresh token's jti is in the blacklist.
    Called by POST /auth/refresh before issuing a new access token.

    Uses idx_blacklisted_tokens_jti index — single fast lookup.
    """
    try:
        stmt   = select(BlacklistedToken).where(BlacklistedToken.jti == jti)
        result = await db.execute(stmt)
        return result.scalars().first() is not None
    except Exception as e:
        logger.error(f"Error checking blacklist: {e}")
        # Fail closed — if we can't check, treat as blacklisted
        return True