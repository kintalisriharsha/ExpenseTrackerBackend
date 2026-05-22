"""
budget_planner_schema.py
────────────────────────
Pydantic v2 schemas for the BudgetScreen.kt planner feature.

Task IDs
────────
Tasks use client-generated UUIDs (str). The Android app generates the UUID
before POSTing so it can reference the task immediately without waiting for
a server response. The server stores and returns whatever UUID the client sends.

Amount cap
──────────
Task budgets are capped at ₹2,00,000 matching the frontend AmountInputField guard.
Weekly budget cap has no upper limit — user decides.

Week date helpers
─────────────────
week_start is always a Monday (ISO weekday 1).
The server enforces this — if the client sends a non-Monday date the server
rounds it down to the nearest Monday automatically.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

TASK_AMOUNT_MAX = 200_000.00


# ── Task sub-model (used inside JSON blob) ─────────────────────────────────────

class TaskItem(BaseModel):
    """
    One task entry stored inside tasks_data JSON.
    id is a client-generated UUID string.
    """
    id       : str   = Field(..., min_length=1, max_length=36)
    name     : str   = Field(..., min_length=1, max_length=255)
    budget   : float = Field(..., ge=0, le=TASK_AMOUNT_MAX)
    is_done  : bool  = Field(False)

    @field_validator("budget")
    @classmethod
    def round_budget(cls, v: float) -> float:
        return round(v, 2)


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class SetWeeklyBudgetRequest(BaseModel):
    """
    Sent when user confirms the WeeklyBudgetDialog.

    Example:
    { "weekly_budget": 5000.0 }
    """
    weekly_budget: float = Field(..., ge=0)

    @field_validator("weekly_budget")
    @classmethod
    def round_budget(cls, v: float) -> float:
        return round(v, 2)


class AddTaskRequest(BaseModel):
    """
    Sent when user taps the + button on a DayBlock and confirms AddTaskBottomSheet.

    day_date must be within the current active week (Mon–Sun).
    id is a client-generated UUID.

    Example:
    {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "day_date": "2026-05-20",
        "name": "Groceries",
        "budget": 800.0
    }
    """
    id       : str   = Field(..., min_length=1, max_length=36)
    day_date : date  = Field(...)
    name     : str   = Field(..., min_length=1, max_length=255)
    budget   : float = Field(..., ge=0, le=TASK_AMOUNT_MAX)

    @field_validator("budget")
    @classmethod
    def round_budget(cls, v: float) -> float:
        return round(v, 2)


class UpdateTaskRequest(BaseModel):
    """
    Sent from EditTaskBottomSheet or when user toggles the checkbox.
    PATCH semantics — only sent fields are updated.

    Example — toggle done:
    { "is_done": true }

    Example — edit name and budget:
    { "name": "Weekly groceries", "budget": 950.0 }

    Example — edit everything:
    { "name": "Groceries", "budget": 900.0, "is_done": false }
    """
    name    : Optional[str]   = Field(None, min_length=1, max_length=255)
    budget  : Optional[float] = Field(None, ge=0, le=TASK_AMOUNT_MAX)
    is_done : Optional[bool]  = Field(None)

    @field_validator("budget")
    @classmethod
    def round_budget(cls, v: float | None) -> float | None:
        return round(v, 2) if v is not None else None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "UpdateTaskRequest":
        if self.name is None and self.budget is None and self.is_done is None:
            raise ValueError("At least one field must be provided.")
        return self


class RolloverRequest(BaseModel):
    """
    Sent by Android WorkManager every Monday morning.

    carry_forward_budget:
        true  → copy last week's weekly_budget into the new active week
                 so user doesn't have to set it again (recommended default)
        false → start new week with weekly_budget = 0

    Example:
    { "carry_forward_budget": true }
    """
    carry_forward_budget: bool = Field(True)


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class DayPlanResponse(BaseModel):
    """One day's tasks — used inside ActiveWeekResponse."""
    day_date : date
    tasks    : list[TaskItem]


class ActiveWeekResponse(BaseModel):
    """
    Returned by GET /budget-planner/active.
    Contains everything BudgetScreen.kt needs to render the full week.

    days is always 7 entries Mon–Sun even if some have empty task lists.
    weekly_exceeded lets Android lock the + button without computing it.

    Example:
    {
        "user_id": 7,
        "week_start": "2026-05-19",
        "week_end": "2026-05-25",
        "weekly_budget": 5000.0,
        "total_spent": 3240.0,
        "weekly_exceeded": false,
        "days": [
            {
                "day_date": "2026-05-19",
                "tasks": [
                    {"id": "uuid1", "name": "Groceries", "budget": 800.0, "is_done": true}
                ]
            },
            { "day_date": "2026-05-20", "tasks": [] },
            ...
        ],
        "updated_at": "2026-05-20T13:30:00Z"
    }
    """
    user_id          : int
    week_start       : date
    week_end         : date
    weekly_budget    : float
    total_spent      : float
    weekly_exceeded  : bool
    days             : list[DayPlanResponse]
    updated_at       : datetime


class TaskResponse(BaseModel):
    """
    Returned after add / update / delete task operations.
    Includes updated totals so Android can refresh the progress bar
    without a second GET call.

    Example:
    {
        "task": {"id": "uuid1", "name": "Groceries", "budget": 800.0, "is_done": true},
        "day_date": "2026-05-19",
        "total_spent": 3240.0,
        "weekly_budget": 5000.0,
        "weekly_exceeded": false
    }
    """
    task             : TaskItem
    day_date         : date
    total_spent      : float
    weekly_budget    : float
    weekly_exceeded  : bool


class DeleteTaskResponse(BaseModel):
    """
    Returned after DELETE /budget-planner/active/tasks/{task_id}.

    Example:
    {
        "deleted_id": "uuid1",
        "total_spent": 2440.0,
        "weekly_budget": 5000.0,
        "weekly_exceeded": false
    }
    """
    deleted_id       : str
    total_spent      : float
    weekly_budget    : float
    weekly_exceeded  : bool


class WeeklyBudgetResponse(BaseModel):
    """
    Returned after PATCH /budget-planner/active/budget.

    Example:
    {
        "weekly_budget": 5000.0,
        "total_spent": 3240.0,
        "weekly_exceeded": false
    }
    """
    weekly_budget   : float
    total_spent     : float
    weekly_exceeded : bool


class HistoryWeekSummary(BaseModel):
    """
    One week's summary for the BudgetHistory tab.
    tasks_data is included so Android can show task breakdown on tap.

    Example:
    {
        "id": 12,
        "week_start": "2026-05-12",
        "week_end": "2026-05-18",
        "weekly_budget": 5000.0,
        "total_spent": 4120.0,
        "within_budget": true,
        "tasks_data": { "2026-05-12": [...], ... },
        "created_at": "2026-05-19T06:00:00Z"
    }
    """
    id             : int
    week_start     : date
    week_end       : date
    weekly_budget  : float
    total_spent    : float
    within_budget  : bool
    tasks_data     : dict
    created_at     : datetime


class HistoryListResponse(BaseModel):
    """
    Returned by GET /budget-planner/history.

    Example:
    {
        "total": 12,
        "weeks": [ { ...HistoryWeekSummary... }, ... ]
    }
    """
    total : int
    weeks : list[HistoryWeekSummary]


class RolloverResponse(BaseModel):
    """
    Returned by POST /budget-planner/rollover.

    action values:
        "rolled_over"    — archive written, active cleared, new week started
        "already_rolled" — rollover already done for this Monday (idempotent)

    Example:
    {
        "action": "rolled_over",
        "archived_week_start": "2026-05-12",
        "new_week_start": "2026-05-19",
        "carried_forward_budget": 5000.0
    }
    """
    action                  : str
    archived_week_start     : Optional[date]
    new_week_start          : date
    carried_forward_budget  : float