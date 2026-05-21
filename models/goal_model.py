"""
goal_model.py
─────────────
One row per savings goal per user.

Fields mapped from the Android frontend (SetGoal.kt / AddGoal composable):
    goal_name      → text name for the goal (e.g. "New Car", "Trip to Bali")
    target_amount  → how much the user wants to save (max ₹2,00,000)
    saved_amount   → running total of amount saved so far (defaults to 0)
    category       → Travel | Home | Electronics | Other
    is_completed   → flipped to True when saved_amount >= target_amount

Indexes:
    - user_id              → almost every query filters by user
    - user_id + category   → category-level breakdown
    - user_id + is_completed → "active goals" vs "completed goals" views
    - created_at           → default sort order (newest first)
"""

from sqlalchemy import (
    Column, BigInteger, Numeric, String, Boolean,
    DateTime, Index, ForeignKey,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db import Base


class Goal(Base):
    __tablename__ = "goals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Core fields (AddGoal composable in SetGoal.kt) ─────────────────
    goal_name     = Column(String(255), nullable=False)
    target_amount = Column(Numeric(12, 2), nullable=False)          # max 2,00,000
    saved_amount  = Column(Numeric(12, 2), nullable=False, default=0.0)

    # Category mirrors goalCategories list in SetGoal.kt
    category = Column(String(50), nullable=False, default="Other")

    # Completion flag — set by backend when saved_amount >= target_amount
    is_completed = Column(Boolean, nullable=False, default=False)

    # ── Audit ──────────────────────────────────────────────────────────
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationship ───────────────────────────────────────────────────
    user = relationship("User", back_populates="goals")

    # ── Indexes ────────────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_goals_user_id",           "user_id"),
        Index("ix_goals_user_category",     "user_id", "category"),
        Index("ix_goals_user_completed",    "user_id", "is_completed"),
        Index("ix_goals_created_at",        "created_at"),
    )

    def __repr__(self):
        return (
            f"<Goal(id={self.id}, user_id={self.user_id}, "
            f"goal_name={self.goal_name!r}, category={self.category})>"
        )