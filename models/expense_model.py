"""
expense_model.py
────────────────
One row per expense entry per user.

Fields mapped directly from the Android frontend:

AddExpense.kt   → amount, category, notes, date, contact_name, contact_number
HistoryScreen   → category, amount, date, time, contact_name
DetailScreen    → all fields above + id (for edit / delete)
HomeScreen      → today's expenses (filtered server-side by date)

Categories enforced (from categories list in AddExpense.kt):
    Food | Transport | Shopping | Leisure | Housing | Health | Education | Other

Amount stored as Numeric(12,2); max 200 000 (enforced in schema, mirroring
the frontend ₹2,00,000 cap in AmountInputField).

Indexes:
    - user_id           → almost every query filters by user
    - user_id + date    → "today's expenses" and history grouping
    - user_id + category→ category breakdown / search
    - created_at        → default sort order (newest first)
"""

from sqlalchemy import (
    Column, BigInteger, Numeric, String, Text,
    DateTime, Index, ForeignKey,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db import Base


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Core fields (AddExpense.kt) ────────────────────────────────────
    amount   = Column(Numeric(12, 2), nullable=False)          # max 200 000.00
    category = Column(String(50),     nullable=False)          # Food / Transport / …
    notes    = Column(Text,           nullable=True,  default="")

    # Date stored as plain DATE; time stored separately as TIME
    # (matches the frontend storing date & time as separate display strings)
    date = Column(DateTime(timezone=True), nullable=False)     # full timestamp; split on read

    # ── Contact (optional — user may skip contact picker) ─────────────
    contact_name   = Column(String(100), nullable=True)
    contact_number = Column(String(20),  nullable=True)

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
    user = relationship("User", back_populates="expenses")

    # ── Indexes ────────────────────────────────────────────────────────
    __table_args__ = (
        # Fastest path for "all expenses for this user" (get_all, search, today)
        Index("ix_expenses_user_id", "user_id"),

        # "Today's expenses" and history grouping by date
        Index("ix_expenses_user_date", "user_id", "date"),

        # Category breakdown / search filter
        Index("ix_expenses_user_category", "user_id", "category"),

        # Default listing order (newest first)
        Index("ix_expenses_created_at", "created_at"),
    )

    def __repr__(self):
        return f"<Expense(id={self.id}, user_id={self.user_id}, amount={self.amount}, category={self.category})>"