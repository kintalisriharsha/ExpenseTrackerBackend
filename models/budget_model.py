"""
budget_planner_model.py
───────────────────────
Two tables for the BudgetScreen.kt planner feature.

WHY TWO TABLES
──────────────
budget_active   → always holds ONLY the current week's live data.
                  One row per user. Cleared every Monday by Android
                  WorkManager via POST /budget-planner/rollover.

budget_history  → append-only archive. One row per user per week.
                  Written during rollover before active is cleared.
                  Never deleted. Read by the BudgetHistory tab.

JSON shape of tasks_data (same in both tables):
{
    "2026-05-19": [
        {"id": "uuid", "name": "Groceries",      "budget": 800.0,  "is_done": true},
        {"id": "uuid", "name": "Uber to office", "budget": 150.0,  "is_done": false}
    ],
    "2026-05-20": [
        {"id": "uuid", "name": "Electricity bill", "budget": 1200.0, "is_done": false}
    ],
    "2026-05-21": [],
    "2026-05-22": [],
    "2026-05-23": [],
    "2026-05-24": [],
    "2026-05-25": []
}

Keys are ISO date strings for all 7 days Mon–Sun.
Each task has a client-generated UUID so the app can reference it
without an extra GET after POST.

total_spent is a denormalized column kept in sync on every task
write so the history endpoint can query it without scanning JSON.

STORAGE ESTIMATE
────────────────
budget_active:   1 row per user          → negligible
budget_history:  52 rows/year/user
                 avg ~1 KB per row (JSON)
                 = ~52 KB/year/user
                 At 1000 users × 10 years = ~520 MB total — fine on Neon.
"""

from sqlalchemy import (
    Column, BigInteger, Numeric, Date, DateTime,
    Index, ForeignKey, JSON, Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db import Base


# ── Table 1: live current week ─────────────────────────────────────────────────

class BudgetActive(Base):
    __tablename__ = "budget_active"

    # user_id IS the PK — exactly one row per user at all times
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    # Monday of the current week
    week_start = Column(Date, nullable=False)

    # Set by the user in WeeklyBudgetDialog; 0 = not set yet
    weekly_budget = Column(Numeric(12, 2), nullable=False, default=0.0)

    # Denormalized sum of all task budgets — updated on every task write
    # so the Android home card can show "Spent X of Y" without scanning JSON
    total_spent = Column(Numeric(12, 2), nullable=False, default=0.0)

    # { "YYYY-MM-DD": [ {id, name, budget, is_done}, ... ] } for all 7 days
    tasks_data = Column(JSON, nullable=False, default=dict)

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    user = relationship("User", back_populates="budget_active")

    def __repr__(self):
        return (
            f"<BudgetActive(user_id={self.user_id}, "
            f"week_start={self.week_start}, "
            f"total_spent={self.total_spent})>"
        )


# ── Table 2: weekly archive ────────────────────────────────────────────────────

class BudgetHistory(Base):
    __tablename__ = "budget_history"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Monday of the archived week
    week_start = Column(Date, nullable=False)

    # Sunday of the archived week — stored for display convenience
    week_end = Column(Date, nullable=False)

    # Snapshot of the weekly budget cap that was set that week
    weekly_budget = Column(Numeric(12, 2), nullable=False, default=0.0)

    # Snapshot of total spent — pre-computed so history queries
    # never need to scan the JSON blob
    total_spent = Column(Numeric(12, 2), nullable=False, default=0.0)

    # Full snapshot of all tasks for that week — never mutated after insert
    tasks_data = Column(JSON, nullable=False, default=dict)

    # When this archive row was created (= when rollover fired)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user = relationship("User", back_populates="budget_history")

    __table_args__ = (
        # One history row per user per week — prevents duplicate archives
        # if WorkManager fires more than once on the same Monday
        Index(
            "uq_budget_history_user_week",
            "user_id", "week_start",
            unique=True,
        ),
        # Fast lookup for history list (newest first per user)
        Index("ix_budget_history_user_id", "user_id"),
    )

    def __repr__(self):
        return (
            f"<BudgetHistory(id={self.id}, user_id={self.user_id}, "
            f"week_start={self.week_start}, total_spent={self.total_spent})>"
        )