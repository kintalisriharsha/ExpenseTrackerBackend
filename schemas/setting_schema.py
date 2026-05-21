from pydantic import BaseModel, Field
from typing import Optional, Literal

# ── Constants ──────────────────────────────────────────────────────────────────

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]

MonthLiteral = Literal[
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
]


def _empty_month_entry() -> dict:
    return {"monthly_budget": 0.0, "daily_limit": 0.0}


# ── Shared sub-schema ──────────────────────────────────────────────────────────

class MonthEntry(BaseModel):
    monthly_budget: float = 0.0
    daily_limit:    float = 0.0


# ── Requests ───────────────────────────────────────────────────────────────────

class SettingsInit(BaseModel):
    monthly_budget:       float = Field(..., ge=0)
    daily_limit:          float = Field(..., ge=0)
    notification_enabled: bool  = False
    is_dark_mode:         bool  = False
    apply_to_all_months:  bool  = False


class SettingsUpdate(BaseModel):
    monthly_budget:       float          = Field(..., ge=0)
    daily_limit:          float          = Field(..., ge=0)
    notification_enabled: bool           = False
    is_dark_mode:         bool           = False
    year:                 Optional[int]  = None
    month:                Optional[MonthLiteral] = None


class CarryForwardRequest(BaseModel):
    overwrite: bool = False


# ── Responses ──────────────────────────────────────────────────────────────────

class SettingsResponse(BaseModel):
    year:                       int
    month:                      str
    monthly_budget:             float
    daily_limit:                float
    notification_enabled:       bool
    is_dark_mode:               bool
    user_monthly_budget_synced: float
    user_daily_limit_synced:    float


class YearSettingsResponse(BaseModel):
    year:   int
    months: dict[str, MonthEntry]


class CarryForwardResponse(BaseModel):
    action:                     str
    target_period:              str
    source_period:              str
    values_applied:             MonthEntry
    already_had:                Optional[MonthEntry] = None
    user_monthly_budget_synced: Optional[float]      = None
    user_daily_limit_synced:    Optional[float]      = None
