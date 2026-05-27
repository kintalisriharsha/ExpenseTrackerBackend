"""
goal_schema.py
──────────────
Pydantic v2 schemas for the goal feature.

Category list mirrors goalCategories in SetGoal.kt:
    Travel | Home | Electronics | Other

Amount cap: 2,00,000 — mirrors the frontend ₹2,00,000 limit.

saved_amount is always returned in responses so the Android UI can
compute progress percentage without an extra call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ── Constants ──────────────────────────────────────────────────────────────────

VALID_CATEGORIES = {
    "Electronics",
    "Home",
    "Travel",
    "Other",
    "Food",
    "Health",
    "Education",
    "Vehicle",
    "Fashion",
    "Entertainment",
    "Investment",
    "Emergency",
}
AMOUNT_MAX = 200_000.00   # ₹2,00,000 — matches frontend cap


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class GoalCreate(BaseModel):
    """
    Sent when the user taps 'Create Goal' in AddGoal (SetGoal.kt).

    Example:
    {
        "goal_name": "New Macbook M4 Air",
        "target_amount": 90000.00,
        "category": "Electronics",
        "saved_amount": 0.0
    }
    """
    goal_name     : str            = Field(..., min_length=1, max_length=255)
    target_amount : float          = Field(..., gt=0, le=AMOUNT_MAX)
    category      : str            = Field("Other")
    saved_amount  : Optional[float] = Field(0.0, ge=0, le=AMOUNT_MAX)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        v = v.strip().title()
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category '{v}'. Allowed: {sorted(VALID_CATEGORIES)}"
            )
        return v

    @field_validator("target_amount", "saved_amount")
    @classmethod
    def round_amount(cls, v: float | None) -> float | None:
        return round(v, 2) if v is not None else None


class GoalUpdate(BaseModel):
    """
    PATCH — all fields optional; only sent fields are updated.
    Triggered from the edit icon on the goal detail screen.

    Example — update just the saved amount (user deposits money):
    {
        "saved_amount": 5000.00
    }

    Example — rename and retarget:
    {
        "goal_name": "MacBook Pro M4",
        "target_amount": 150000.00
    }
    """
    goal_name     : Optional[str]   = Field(None, min_length=1, max_length=255)
    target_amount : Optional[float] = Field(None, gt=0, le=AMOUNT_MAX)
    saved_amount  : Optional[float] = Field(None, ge=0, le=AMOUNT_MAX)
    category      : Optional[str]   = Field(None)
    is_completed  : Optional[bool]  = Field(None)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().title()
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category '{v}'. Allowed: {sorted(VALID_CATEGORIES)}"
            )
        return v

    @field_validator("target_amount", "saved_amount")
    @classmethod
    def round_amounts(cls, v: float | None) -> float | None:
        return round(v, 2) if v is not None else None


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class GoalResponse(BaseModel):
    """
    Returned for every single-goal operation (create, update, get by id).

    `progress_pct` is pre-computed on the server so the Android circular
    progress indicator (CircularIndicator.kt) can use it directly.

    Example:
    {
        "id": 3,
        "user_id": 7,
        "goal_name": "New Macbook M4 Air",
        "target_amount": 90000.0,
        "saved_amount": 0.0,
        "progress_pct": 0.0,
        "category": "Electronics",
        "is_completed": false,
        "created_at": "2026-05-20T13:30:00Z",
        "updated_at": "2026-05-20T13:30:00Z"
    }
    """
    id            : int
    user_id       : int
    goal_name     : str
    target_amount : float
    saved_amount  : float
    progress_pct  : float      # 0.0 – 100.0, pre-computed
    category      : str
    is_completed  : bool
    created_at    : datetime
    updated_at    : datetime

    model_config = {"from_attributes": True}


class GoalListResponse(BaseModel):
    """
    Returned by GET /goals and GET /goals?limit=.

    `total` is the grand total count across all pages.

    Example:
    {
        "total": 5,
        "goals": [ { ...GoalResponse... }, ... ]
    }
    """
    total : int
    goals : list[GoalResponse]
