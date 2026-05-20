"""
setting_model.py
────────────────
One row per user — user_id IS the primary key.
Since each user has exactly one settings record, a surrogate `id` is
redundant; using user_id directly as PK keeps the schema simpler and
removes the need for a separate UNIQUE constraint.

JSON shape of `budget_data`:
{
    "2026": {
        "jan": { "monthly_budget": 4500.0, "daily_limit": 150.0 },
        "feb": { "monthly_budget": 4200.0, "daily_limit": 130.0 },
        ...
        "dec": { "monthly_budget": 5000.0, "daily_limit": 160.0 }
    }
}

Carry-forward rule (enforced in CRUD):
    When a new month starts and that month has no entry yet,
    the previous month's values are copied in automatically.

Settings screen saves all four fields in one tap:
    - monthly_budget
    - daily_limit
    - notification_enabled
    - is_dark_mode
"""

from sqlalchemy import Column, BigInteger, Boolean, ForeignKey, DateTime, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db import Base


class Settings(Base):
    __tablename__ = "user_budgets"

    # user_id is both the PK and the FK — one row per user, no surrogate key needed
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    # year → month → { monthly_budget, daily_limit }
    budget_data = Column(JSON, nullable=False, default=lambda: {})

    # ── Settings screen preferences ───────────────────────────────────
    notification_enabled = Column(Boolean, nullable=False, default=False)
    is_dark_mode         = Column(Boolean, nullable=False, default=False)

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    user = relationship("User", back_populates="settings")

    def __repr__(self):
        return f"<Settings(user_id={self.user_id})>"