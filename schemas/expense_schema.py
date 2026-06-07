"""
expense_schema.py
─────────────────
Pydantic v2 schemas for the expense feature.

Category list mirrors AddExpense.kt exactly:
    Food | Transport | Shopping | Leisure | Housing | Health | Education | Other

Amount cap: 200 000.00 — mirrors the frontend ₹2,00,000 limit
in AmountInputField's onValueChange guard.

Date/time:
    - Stored as a single UTC DateTime in the DB.
    - Returned as separate `date` (DD MMM YYYY) and `time` (HH:MM AM/PM)
      strings so the Android UI can render them directly without
      any client-side formatting.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ── Constants ──────────────────────────────────────────────────────────────────

VALID_CATEGORIES = {
    "Food", "Transport", "Shopping",
    "Leisure", "Housing", "Health", "Education", "Other",
}

AMOUNT_MAX = 200_000.00   # ₹2,00,000 — matches frontend cap


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class ExpenseCreate(BaseModel):
    """
    Sent when the user taps 'Add Expense' in AddExpense.kt.

    Example:
    {
        "amount": 450.00,
        "category": "Food",
        "notes": "Lunch at Saravana Bhavan",
        "date": "2026-05-20T13:30:00Z",
        "time": "09:05",
        "contact_name": "Rahul Kumar",
        "contact_number": "9876543210"
    }
    """
    amount         : float           = Field(..., gt=0, le=AMOUNT_MAX)
    category       : str             = Field(...)
    notes          : Optional[str]   = Field(None, max_length=500)
    date           : datetime        = Field(..., description="ISO-8601 UTC timestamp")
    time           : dtime           = Field(..., description="Time of expense in HH:MM format")
    contact_name   : Optional[str]   = Field(None, max_length=100)
    contact_number : Optional[str]   = Field(None, max_length=20)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        # Title-case to be forgiving ("food" → "Food")
        v = v.strip().title()
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category '{v}'. "
                f"Allowed: {sorted(VALID_CATEGORIES)}"
            )
        return v

    @field_validator("amount")
    @classmethod
    def round_amount(cls, v: float) -> float:
        return round(v, 2)


class ExpenseUpdate(BaseModel):
    """
    Sent from the edit screen (edit icon in DetailScreen.kt → EditExpense).
    All fields are optional — only supplied fields are updated (PATCH semantics).

    Example — update just the amount and notes:
    {
        "amount": 500.00,
        "notes": "Dinner, not lunch"
    }

    Example — update everything:
    {
        "amount": 120.00,
        "category": "Transport",
        "notes": "Ola cab to airport",
        "date": "2026-05-20T18:00:00Z",
        "time": "18:00",
        "contact_name": "Driver Suresh",
        "contact_number": "9123456789"
    }
    """
    amount         : Optional[float]    = Field(None, gt=0, le=AMOUNT_MAX)
    category       : Optional[str]      = Field(None)
    notes          : Optional[str]      = Field(None, max_length=500)
    date           : Optional[datetime] = Field(None)
    time           : Optional[dtime]    = Field(None)
    contact_name   : Optional[str]      = Field(None, max_length=100)
    contact_number : Optional[str]      = Field(None, max_length=20)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().title()
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category '{v}'. "
                f"Allowed: {sorted(VALID_CATEGORIES)}"
            )
        return v

    @field_validator("amount")
    @classmethod
    def round_amount(cls, v: float | None) -> float | None:
        return round(v, 2) if v is not None else None


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class ExpenseResponse(BaseModel):
    """
    Returned for every single-expense operation (create, update, get by id).

    `date` and `time` are pre-formatted strings so the Android UI can
    display them directly without client-side formatting.

    Example:
    {
        "id": 42,
        "user_id": 7,
        "amount": 450.00,
        "category": "Food",
        "notes": "Lunch at Saravana Bhavan",
        "date": "20 May 2026",
        "time": "09:05",
        "contact_name": "Rahul Kumar",
        "contact_number": "9876543210",
        "created_at": "2026-05-20T13:30:00Z",
        "updated_at": "2026-05-20T13:30:00Z"
    }
    """
    id             : int
    user_id        : int
    amount         : float
    category       : str
    notes          : Optional[str]
    date           : str    # "20 May 2026"
    time           : str    # "09:05"
    contact_name   : Optional[str]
    contact_number : Optional[str]
    created_at     : datetime
    updated_at     : datetime

    model_config = {"from_attributes": True}


class ExpenseListResponse(BaseModel):
    """
    Returned by GET /expenses and GET /expenses/today.

    `total` lets the UI know how many records exist in total
    (useful for "50 of 120" pagination labels).

    Example:
    {
        "total": 120,
        "expenses": [ { ...ExpenseResponse... }, ... ]
    }
    """
    total    : int
    expenses : list[ExpenseResponse]