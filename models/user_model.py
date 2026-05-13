from sqlalchemy import (
    Column, BigInteger, String, Numeric, DateTime,
    Boolean, Integer, Index, Text, Enum
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
    mobile_number  = Column(String(20),  nullable=True, unique=True)   # ← NEW: stored without formatting
    auth_provider  = Column(Enum(AuthProvider), nullable=False, default=AuthProvider.email)
    email_verified = Column(Boolean, nullable=False, default=False)

    # OTP fields — email provider only; always NULL for Google users
    hashed_otp     = Column(Text,    nullable=True)
    otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    otp_attempts   = Column(Integer, nullable=False, default=0)        # ← NEW: brute-force counter

    # Google SSO
    google_sub = Column(Text, nullable=True, unique=True)

    daily_budget   = Column(Numeric(12, 2), nullable=False, default=0.0)
    monthly_budget = Column(Numeric(12, 2), nullable=False, default=0.0)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # updated_at has no onupdate= because that is unreliable in async ORM.
    # Every CRUD write sets it manually: user.updated_at = datetime.now(timezone.utc)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_users_email",         "email"),
        Index("idx_users_google_sub",    "google_sub"),
        Index("idx_users_mobile_number", "mobile_number"),
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
        Index("idx_blacklisted_tokens_expires", "expires_at"),
    )

    def __repr__(self):
        return f"<BlacklistedToken(jti={self.jti}, user_id={self.user_id})>"
