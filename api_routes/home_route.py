"""
home_route.py
─────────────
Single aggregated endpoint that powers HomeScreen.kt.

Why one endpoint?
    HomeScreen renders three independent sections in a single scroll view:
        1. DailySummaryCard   → today's expenses + budget totals
        2. TodayExpensesSection → up to 3 most-recent today's expenses
        3. SavingsBanner      → top active savings goal (closest to completion)

    A single GET /home call lets the Android app load the screen in one
    network round-trip instead of firing three parallel requests.

Endpoint
────────
GET /home   → HomeResponse
    ├── budget   : BudgetSummary       (WeeklyBudgetCard / DailySummaryCard)
    ├── expenses : HomExpenseSummary   (TodayExpensesSection / DailySummaryCard)
    └── goal     : HomeGoalSummary | None   (SavingsBanner — null if no active goal)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import cast, case, func, select
from sqlalchemy import Date as SADate
from sqlalchemy.ext.asyncio import AsyncSession

from auth.auth import get_current_user
from db import get_db
from models.budget_model import BudgetActive
from models.expense_model import Expense
from models.goal_model import Goal

import asyncio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/home", tags=["home"])


# ══════════════════════════════════════════════════════════════════════════════
# Response schemas (home-specific, no separate schema file needed)
# ══════════════════════════════════════════════════════════════════════════════

class HomeBudgetSummary(BaseModel):
    """
    Powers the DailySummaryCard and the weekly progress ring.

    weekly_budget   → cap set by user in WeeklyBudgetDialog (0 = not set yet)
    total_spent     → denormalised sum already stored in budget_active
    remaining       → weekly_budget - total_spent  (can be negative if exceeded)
    weekly_exceeded → true when total_spent > weekly_budget (and budget > 0)
    week_start      → Monday of the current week ("2026-05-19")
    week_end        → Sunday of the current week ("2026-05-25")

    Example:
    {
        "weekly_budget": 5000.0,
        "total_spent": 950.0,
        "remaining": 4050.0,
        "weekly_exceeded": false,
        "week_start": "2026-05-19",
        "week_end": "2026-05-25"
    }
    """
    weekly_budget   : float
    total_spent     : float
    remaining       : float
    weekly_exceeded : bool
    week_start      : Optional[date]  # null if budget row not yet initialised
    week_end        : Optional[date]


class HomeExpenseItem(BaseModel):
    """
    One expense row in the TodayExpensesSection card list.
    Matches the ExpenseItem data class used in HomeScreen.kt.

    `time` is pre-formatted ("01:30 PM") so Android can display directly.

    Example:
    {
        "id": 42,
        "notes": "Lunch at Saravana Bhavan",
        "category": "Food",
        "amount": 450.0,
        "time": "01:30 PM"
    }
    """
    id       : int
    notes    : Optional[str]
    category : str
    amount   : float
    time     : str    # "01:30 PM"


class HomeExpenseSummary(BaseModel):
    """
    Powers DailySummaryCard ("Spent Today") + TodayExpensesSection list.

    spent_today  → sum of today's expense amounts (for the card header)
    total_today  → count of today's expenses (for "3 transactions" label)
    expenses     → up to 3 most-recent today's expenses (for the card list)

    Example:
    {
        "spent_today": 485.0,
        "total_today": 3,
        "expenses": [
            {"id": 42, "notes": "Lunch", "category": "Food", "amount": 450.0, "time": "01:30 PM"},
            {"id": 41, "notes": "Uber",  "category": "Transport", "amount": 15.0, "time": "10:00 AM"},
            {"id": 40, "notes": "Tea",   "category": "Food", "amount": 20.0, "time": "09:00 AM"}
        ]
    }
    """
    spent_today : float
    total_today : int
    expenses    : list[HomeExpenseItem]   # max 3 — matches TodayExpensesSection


class HomeGoalSummary(BaseModel):
    """
    Powers SavingsBanner — the active savings goal closest to completion.

    Returns null in HomeResponse if the user has no active goals.

    progress_pct is pre-computed (0.0–100.0) so CircularProgress.kt
    can use it directly without client-side arithmetic.

    Example:
    {
        "id": 3,
        "goal_name": "New Macbook M4 Air",
        "target_amount": 90000.0,
        "saved_amount": 12000.0,
        "progress_pct": 13.33,
        "category": "Electronics"
    }
    """
    id            : int
    goal_name     : str
    target_amount : float
    saved_amount  : float
    progress_pct  : float
    category      : str


class HomeResponse(BaseModel):
    """
    Root response for GET /home.

    Aggregates everything HomeScreen.kt needs in a single payload:
        budget   → DailySummaryCard + WeeklyBudgetCard progress ring
        expenses → DailySummaryCard spent total + TodayExpensesSection list
        goal     → SavingsBanner (null when no active goal exists)

    Example:
    {
        "budget": {
            "weekly_budget": 5000.0,
            "total_spent": 950.0,
            "remaining": 4050.0,
            "weekly_exceeded": false,
            "week_start": "2026-05-19",
            "week_end": "2026-05-25"
        },
        "expenses": {
            "spent_today": 485.0,
            "total_today": 3,
            "expenses": [ { ...HomeExpenseItem... } ]
        },
        "goal": {
            "id": 3,
            "goal_name": "New Macbook M4 Air",
            "target_amount": 90000.0,
            "saved_amount": 12000.0,
            "progress_pct": 13.33,
            "category": "Electronics"
        }
    }
    """
    budget   : HomeBudgetSummary
    expenses : HomeExpenseSummary
    goal     : Optional[HomeGoalSummary]   # null → hide SavingsBanner


# ══════════════════════════════════════════════════════════════════════════════
# Private helpers
# ══════════════════════════════════════════════════════════════════════════════

def _week_end(week_start: date) -> date:
    """Return the Sunday (day 6) of the week that starts on Monday week_start."""
    from datetime import timedelta
    return week_start + timedelta(days=6)


def _calc_progress(saved: float, target: float) -> float:
    """Return 0.0–100.0, guarded against division by zero."""
    if target <= 0:
        return 0.0
    return round(min(saved / target * 100, 100.0), 2)


def _fmt_time(dt: datetime) -> str:
    """Format a UTC datetime as '01:30 PM' for direct display in Android."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%I:%M %p")


# ══════════════════════════════════════════════════════════════════════════════
# Data fetchers (each is a focused async function — easy to unit-test)
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_budget(db: AsyncSession, user_id: int) -> HomeBudgetSummary:
    """
    Read the user's budget_active row.
    Returns zeroed-out summary if the row doesn't exist yet (first-time user).
    """
    result = await db.execute(
        select(BudgetActive).where(BudgetActive.user_id == user_id)
    )
    active = result.scalars().first()

    if not active:
        # User has never opened BudgetScreen — return safe defaults.
        return HomeBudgetSummary(
            weekly_budget   = 0.0,
            total_spent     = 0.0,
            remaining       = 0.0,
            weekly_exceeded = False,
            week_start      = None,
            week_end        = None,
        )

    weekly    = float(active.weekly_budget)
    spent     = float(active.total_spent)
    remaining = round(weekly - spent, 2)
    exceeded  = (weekly > 0) and (spent > weekly)

    return HomeBudgetSummary(
        weekly_budget   = weekly,
        total_spent     = spent,
        remaining       = remaining,
        weekly_exceeded = exceeded,
        week_start      = active.week_start,
        week_end        = _week_end(active.week_start),
    )


async def _fetch_expenses(db: AsyncSession, user_id: int) -> HomeExpenseSummary:
    """
    Return today's expenses (server UTC date).
    Computes spent_today sum and returns the 3 most recent rows for the card list.
    Uses ix_expenses_user_date.
    """
    today = date.today()

    result = await db.execute(
        select(Expense)
        .where(
            Expense.user_id == user_id,
            cast(Expense.date, SADate) == today,
        )
        .order_by(Expense.date.desc())
    )
    all_today = result.scalars().all()

    spent_today = round(sum(float(e.amount) for e in all_today), 2)

    # Cap at 3 for the HomeScreen card list
    recent_3 = all_today[:3]
    expense_items = [
        HomeExpenseItem(
            id       = e.id,
            notes    = e.notes or "",
            category = e.category,
            amount   = float(e.amount),
            time     = _fmt_time(e.date),
        )
        for e in recent_3
    ]

    return HomeExpenseSummary(
        spent_today = spent_today,
        total_today = len(all_today),
        expenses    = expense_items,
    )


async def _fetch_goal(db: AsyncSession, user_id: int) -> Optional[HomeGoalSummary]:
    """
    Return the active savings goal closest to completion for the SavingsBanner.

    Ordering: (saved_amount / target_amount) DESC — the goal with the highest
    completion ratio is shown first, motivating the user to finish it.

    A CASE guard prevents division-by-zero when target_amount is 0;
    such rows sort to the bottom (ratio treated as 0).

    Returns None if the user has no active goals.
    Uses ix_goals_user_completed.
    """
    # Completion ratio expression — safe against target_amount = 0
    completion_ratio = case(
        (Goal.target_amount > 0, Goal.saved_amount / Goal.target_amount),
        else_=0,
    )

    result = await db.execute(
        select(Goal)
        .where(
            Goal.user_id      == user_id,
            Goal.is_completed == False,   # noqa: E712
        )
        .order_by(completion_ratio.desc())   # closest to 100 % first
        .limit(1)
    )
    goal = result.scalars().first()

    if not goal:
        return None

    saved  = float(goal.saved_amount)
    target = float(goal.target_amount)

    return HomeGoalSummary(
        id            = goal.id,
        goal_name     = goal.goal_name,
        target_amount = target,
        saved_amount  = saved,
        progress_pct  = _calc_progress(saved, target),
        category      = goal.category,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "",
    response_model=HomeResponse,
    status_code=status.HTTP_200_OK,
    summary="Home screen — budget, today's expenses, and top savings goal in one call",
    description="""
Called every time **HomeScreen.kt** mounts or resumes.

Returns a single payload containing everything the screen needs:

| Section | Field | Used by |
|---|---|---|
| DailySummaryCard | `budget.weekly_budget` | "Daily Budget" row |
| DailySummaryCard | `budget.total_spent` | Progress ring fill |
| DailySummaryCard | `budget.remaining` | "Remaining" amount |
| DailySummaryCard | `expenses.spent_today` | "Spent Today" header |
| TodayExpensesSection | `expenses.expenses` | 3-card expense list |
| SavingsBanner | `goal.*` | Goal name, saved/target, progress |

**Goal selection:** the goal with the highest `saved_amount / target_amount`
ratio is returned — i.e. the one closest to completion. This maximises
motivation by showing the user the goal they are about to achieve.
Goals with `target_amount = 0` are treated as 0 % and sort to the bottom.

**`goal` is `null`** when the user has no active savings goals — the
Android app should hide the `SavingsBanner` composable in that case.

**`budget.week_start` / `week_end` are `null`** when the user has never
opened the Budget Planner — the Android app should hide the budget ring
or show a "Set your budget" prompt.

Example response:
```json
{
    "budget": {
        "weekly_budget": 5000.0,
        "total_spent": 950.0,
        "remaining": 4050.0,
        "weekly_exceeded": false,
        "week_start": "2026-05-19",
        "week_end": "2026-05-25"
    },
    "expenses": {
        "spent_today": 485.0,
        "total_today": 3,
        "expenses": [
            {"id": 42, "notes": "Lunch at Saravana Bhavan", "category": "Food",      "amount": 450.0, "time": "01:30 PM"},
            {"id": 41, "notes": "Uber to office",           "category": "Transport", "amount": 15.0,  "time": "10:00 AM"},
            {"id": 40, "notes": "Morning tea",              "category": "Food",      "amount": 20.0,  "time": "09:00 AM"}
        ]
    },
    "goal": {
        "id": 3,
        "goal_name": "New Macbook M4 Air",
        "target_amount": 90000.0,
        "saved_amount": 12000.0,
        "progress_pct": 13.33,
        "category": "Electronics"
    }
}
```
""",
)
async def get_home_route(
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
) -> HomeResponse:
    user_id = current_user["id"]

    # Three independent DB reads — each hits a different table/index.
    # All three are awaited sequentially; switch to asyncio.gather if
    # sub-millisecond latency becomes a concern at scale.

    budget, expenses, goal = await asyncio.gather(
        _fetch_budget(db, user_id),
        _fetch_expenses(db, user_id),
        _fetch_goal(db, user_id)
    )

    logger.info(
        f"Home loaded: user_id={user_id} "
        f"spent_today={expenses.spent_today} "
        f"active_goal={'yes' if goal else 'none'}"
    )

    return HomeResponse(budget=budget, expenses=expenses, goal=goal)