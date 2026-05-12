from sqlalchemy import (
    Column, BigInteger, String, Numeric, DateTime,
    Boolean, Index, Text, Enum
)
from sqlalchemy.sql import func
import enum
from db import Base


class AuthProvider(str, enum.Enum):
    email  = "email"
    google = "google"


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

    # Google sub (provider's unique user ID) — NULL for email users
    google_sub     = Column(Text, nullable=True, unique=True)

    daily_budget   = Column(Numeric(12, 2), nullable=False, default=0.0)
    monthly_budget = Column(Numeric(12, 2), nullable=False, default=0.0)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
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
