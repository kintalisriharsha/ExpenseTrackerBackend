from sqlalchemy import Column, BigInteger, String, Numeric, DateTime, Index, Text
from sqlalchemy.sql import func
from db import Base


class User(Base):
    __tablename__ = "users"

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    firebase_uid   = Column(Text, nullable=False, unique=True)
    phone_number   = Column(String(20), nullable=False, unique=True)
    display_name   = Column(String(255), nullable=True)
    daily_budget   = Column(Numeric(12, 2), nullable=False, default=0.0)
    monthly_budget = Column(Numeric(12, 2), nullable=False, default=0.0)
    created_at     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # login lookup — most frequent query
        Index("idx_users_firebase_uid", "firebase_uid"),
        # phone number search
        Index("idx_users_phone_number", "phone_number"),
    )

    def __repr__(self):
        return f"<User(id={self.id}, phone={self.phone_number}, firebase_uid={self.firebase_uid})>"


class BlacklistedToken(Base):
    __tablename__ = "blacklisted_tokens"
 
    id         = Column(BigInteger, primary_key=True, autoincrement=True)
    jti        = Column(Text, nullable=False, unique=True)   # JWT ID — not the full token
    user_id    = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)  # copied from token exp
 
    __table_args__ = (
        # checked on every /auth/refresh — must be fast
        Index("idx_blacklisted_tokens_jti", "jti"),
        # useful for "logout all sessions" later
        Index("idx_blacklisted_tokens_user_id", "user_id"),
    )
 
    def __repr__(self):
        return f"<BlacklistedToken(jti={self.jti}, user_id={self.user_id})>"