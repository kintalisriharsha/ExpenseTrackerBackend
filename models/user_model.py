"""
user_model.py
─────────────
User, BlacklistedToken, and the SQLAlchemy event that automatically
creates a Settings row (with default data) whenever a new User is inserted.

The `after_flush` event fires inside the same transaction as the INSERT,
so the settings row is committed together with the user — no separate
init call needed in the auth CRUD.
"""

from sqlalchemy import (
    Column, BigInteger, String, Numeric, DateTime,
    Boolean, Index, Text, Enum, event, Integer, text
)
from sqlalchemy.orm import relationship, Session
from sqlalchemy.sql import func
import enum
from datetime import date

from db import Base


# ── Helpers (mirrors MONTHS from setting_schema to avoid circular import) ─────

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]


def _default_budget_data() -> dict:
    """Seed the current month with zero values so the Settings screen loads cleanly."""
    today = date.today()
    year_str  = str(today.year)
    month_str = _MONTHS[today.month - 1]
    return {
        year_str: {
            month_str: {"monthly_budget": 0.0,"weekly_budget":0.0, "daily_limit": 0.0}
        }
    }


# ── Enums ─────────────────────────────────────────────────────────────────────

class AuthProvider(str, enum.Enum):
    email  = "email"
    google = "google"


# ── Models ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    email          = Column(String(255), nullable=False, unique=True)
    display_name   = Column(String(255), nullable=True)
    auth_provider  = Column(Enum(AuthProvider), nullable=False, default=AuthProvider.email)
    email_verified = Column(Boolean, nullable=False, default=False)

    # OTP fields — only used for email provider; NULL for Google users
    hashed_otp     = Column(Text, nullable=True)
    otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    otp_attempts   = Column(Integer, nullable=False, default=0, server_default=text("0"))

    # Optional profile field used by the auth flow
    mobile_number  = Column(String(20), nullable=True, unique=True)

    # Google sub (provider's unique user ID) — NULL for email users
    google_sub     = Column(Text, nullable=True, unique=True)

    # Denormalised budget columns — kept in sync by setting_crud._sync_to_user()
    # and also embedded in the JWT so every request has them without a DB hit.
    daily_budget   = Column(Numeric(12, 2), nullable=False, default=0.0)
    weekly_budget = Column(Numeric(12, 2), nullable=False, default=0.0, server_default="0.0")
    monthly_budget = Column(Numeric(12, 2), nullable=False, default=0.0)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # One-to-one back-reference to Settings
    settings = relationship(
        "Settings",
        back_populates="user",
        uselist=False,          # one-to-one
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    expenses = relationship(
        "Expense",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    goals = relationship(
        "Goal",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    budget_active = relationship(
        "BudgetActive",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
 
    budget_history = relationship(
        "BudgetHistory",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("idx_users_email",      "email"),
        Index("idx_users_google_sub", "google_sub"),
    )

    def __repr__(self):
        return f"<User(id={self.id}, email={self.email}, provider={self.auth_provider})>"


class BlacklistedToken(Base):
    __tablename__ = "blacklisted_tokens"

    id         = Column(BigInteger, primary_key=True, autoincrement=True)
    jti        = Column(Text, nullable=False, unique=True)
    user_id    = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_blacklisted_tokens_jti",     "jti"),
        Index("idx_blacklisted_tokens_user_id", "user_id"),
    )

    def __repr__(self):
        return f"<BlacklistedToken(jti={self.jti}, user_id={self.user_id})>"


# ── Auto-create Settings on new User ─────────────────────────────────────────
# after_flush fires inside the same transaction as the User INSERT, so both
# rows are committed atomically — no separate /budget/init call needed.

@event.listens_for(User, "after_insert")
def _auto_create_settings(mapper, connection, target: User):
    """
    Runs synchronously inside SQLAlchemy's flush pipeline.
    Imports Settings here (not at module top) to avoid a circular import
    between user_model ↔ setting_model.
    """
    from models.setting_model import Settings  # local import — intentional

    connection.execute(
        Settings.__table__.insert().values(
            user_id              = target.id,
            budget_data          = _default_budget_data(),
            notification_enabled = False,
            is_dark_mode         = False,
        )
    )