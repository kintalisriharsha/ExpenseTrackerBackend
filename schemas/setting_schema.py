"""
budget_schema.py
────────────────
Pydantic v2 schemas for the budget feature.

JSON structure:
    year → month → { monthly_budget, daily_limit }

Settings screen sends all fields in one save:
    monthly_budget + daily_limit + notification_enabled + is_dark_mode
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime

# ── Constants ──────────────────────────────────────────────────────────────────

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]


def _empty_month_entry() -> dict:
    return {"monthly_budget": 0.0, "daily_limit": 0.0}


# ══════════════════════════════════════════════════════════════════════════════
# SUB-MODELS
# ══════════════════════════════════════════════════════════════════════════════

class MonthEntry(BaseModel):
    """Budget values stored per month."""
    monthly_budget : float = Field(..., ge=0)
    daily_limit    : float = Field(..., ge=0)


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class BudgetInit(BaseModel):
    """
    Called once at onboarding / first Settings save.
    Seeds the current month with budget + preferences.

    Example:
    {
        "monthly_budget": 4500.0,
        "daily_limit": 150.0,
        "notification_enabled": true,
        "is_dark_mode": false,
        "apply_to_all_months": true
    }
    """
    monthly_budget      : float = Field(..., ge=0)
    daily_limit         : float = Field(..., ge=0)
    notification_enabled: bool  = Field(False)
    is_dark_mode        : bool  = Field(False)
    apply_to_all_months : bool  = Field(
        False,
        description="Seed every month of the current year with these budget values"
    )


class BudgetUpdate(BaseModel):
    """
    Sent every time the user taps 'Save All Settings'.
    All four Settings screen fields in one request.

    Budget fields update the JSON blob.
    Preference fields update the boolean columns directly.

    year + month are optional — defaults to current month if omitted.

    Example:
    {
        "monthly_budget": 4800.0,
        "daily_limit": 160.0,
        "notification_enabled": true,
        "is_dark_mode": true
    }
    """
    monthly_budget      : float          = Field(..., ge=0)
    daily_limit         : float          = Field(..., ge=0)
    notification_enabled: bool           = Field(...)
    is_dark_mode        : bool           = Field(...)
    year                : Optional[int]  = Field(None, ge=2000, le=2100)
    month               : Optional[str]  = Field(None)

    @field_validator("month")
    @classmethod
    def validate_month(cls, v):
        if v is not None:
            v = v.lower()
            if v not in MONTHS:
                raise ValueError(f"Invalid month '{v}'. Allowed: {MONTHS}")
        return v


class CarryForwardRequest(BaseModel):
    """
    Sent by Android WorkManager at the start of each new month.
    Only carries forward budget values — preferences stay as-is.
    """
    overwrite: bool = Field(False)


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class BudgetResponse(BaseModel):
    """Full record — used internally, not exposed directly to the app."""
    id                   : int
    user_id              : int
    budget_data          : dict
    notification_enabled : bool
    is_dark_mode         : bool
    created_at           : datetime
    updated_at           : datetime

    model_config = {"from_attributes": True}


class SettingsResponse(BaseModel):
    """
    Returned to the Android Settings screen on load and after every save.
    Contains everything the screen needs to render itself.

    Example:
    {
        "year": 2026,
        "month": "may",
        "monthly_budget": 4500.0,
        "daily_limit": 150.0,
        "notification_enabled": true,
        "is_dark_mode": false,
        "user_monthly_budget_synced": 4500.0,
        "user_daily_limit_synced": 150.0
    }
    """
    year                       : int
    month                      : str
    monthly_budget             : float
    daily_limit                : float
    notification_enabled       : bool
    is_dark_mode               : bool
    user_monthly_budget_synced : float
    user_daily_limit_synced    : float


class YearBudgetResponse(BaseModel):
    """All months for a given year."""
    year   : int
    months : dict[str, MonthEntry]


class CarryForwardResponse(BaseModel):
    """
    action values:
        "carried_forward" — month was empty, previous month copied in
        "already_set"     — month already had values, nothing changed
        "overwritten"     — had values but overwrite=true forced replace
    
    Note: notification_enabled and is_dark_mode are never carry-forwarded —
    they are sticky preferences that only change when the user explicitly
    toggles them in Settings.
    """
    action         : str
    target_period  : str
    source_period  : str
    values_applied : MonthEntry
    already_had    : Optional[MonthEntry] = None
    user_monthly_budget_synced : Optional[float] = None
    user_daily_limit_synced    : Optional[float] = None