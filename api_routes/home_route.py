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
    ├── budget   : HomeBudgetSummary   (WeeklyBudgetCard / DailySummaryCard)
    ├── expenses : HomeExpenseSummary  (TodayExpensesSection / DailySummaryCard)
    └── goal     : HomeGoalSummary | None  (SavingsBanner — null if no active goal)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import cast, case, select
from sqlalchemy import Date as SADate
from sqlalchemy.ext.asyncio import AsyncSession

from auth.auth import get_current_user
from db import get_db, AsyncSessionLocal
from models.budget_model import BudgetActive
from models.expense_model import Expense
from models.goal_model import Goal
from models.setting_model import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/home", tags=["home"])

# Month abbreviations — mirrors setting_crud.py
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]


# ══════════════════════════════════════════════════════════════════════════════
# Response schemas
# ══════════════════════════════════════════════════════════════════════════════

class HomeBudgetSummary(BaseModel):
    # ── from Settings (single source of truth) ────────────────────────
    weekly_budget   : float
    daily_limit     : float          # ← NEW
    monthly_budget  : float          # ← NEW
    # ── from BudgetActive (live week data) ───────────────────────────
    total_spent     : float
    weekly_remaining: float
    weekly_exceeded : bool
    week_start      : Optional[date]
    week_end        : Optional[date]


class HomeExpenseItem(BaseModel):
    id       : int
    notes    : Optional[str]
    category : str
    amount   : float
    time     : str    # "01:30 PM"


class HomeExpenseSummary(BaseModel):
    spent_today : float
    total_today : int
    expenses    : list[HomeExpenseItem]


class HomeGoalSummary(BaseModel):
    id            : int
    goal_name     : str
    target_amount : float
    saved_amount  : float
    progress_pct  : float
    category      : str


class HomeResponse(BaseModel):
    budget   : HomeBudgetSummary
    expenses : HomeExpenseSummary
    goal     : Optional[HomeGoalSummary]


# ══════════════════════════════════════════════════════════════════════════════
# Private helpers
# ══════════════════════════════════════════════════════════════════════════════

def _week_end(week_start: date) -> date:
    return week_start + timedelta(days=6)


def _calc_progress(saved: float, target: float) -> float:
    if target <= 0:
        return 0.0
    return round(min(saved / target * 100, 100.0), 2)


def _fmt_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%I:%M %p")


def _get_current_month_entry(budget_data: dict) -> dict:
    """Read current month's entry from Settings.budget_data."""
    today     = date.today()
    year_str  = str(today.year)
    month_str = MONTHS[today.month - 1]
    return budget_data.get(year_str, {}).get(month_str, {})


# ══════════════════════════════════════════════════════════════════════════════
# Data fetchers — each opens its OWN session for concurrent asyncio.gather
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_budget(user_id: int) -> HomeBudgetSummary:
    """
    Reads budget limits from Settings (single source of truth)
    and live totals from BudgetActive.
    """
    async with AsyncSessionLocal() as db:
        # ── Settings: weekly_budget, daily_limit, monthly_budget ──────
        settings_result = await db.execute(
            select(Settings).where(Settings.user_id == user_id)
        )
        settings = settings_result.scalars().first()

        # ── BudgetActive: total_spent, week_start ─────────────────────
        active_result = await db.execute(
            select(BudgetActive).where(BudgetActive.user_id == user_id)
        )
        active = active_result.scalars().first()

    # ── Extract limits from Settings ──────────────────────────────────
    if settings:
        entry          = _get_current_month_entry(settings.budget_data)
        weekly_budget  = float(entry.get("weekly_budget",  0.0))
        daily_limit    = float(entry.get("daily_limit",    0.0))
        monthly_budget = float(entry.get("monthly_budget", 0.0))
    else:
        weekly_budget  = 0.0
        daily_limit    = 0.0
        monthly_budget = 0.0

    # ── Extract live totals from BudgetActive ─────────────────────────
    if active:
        total_spent      = float(active.total_spent)
        week_start       = active.week_start
        week_end         = _week_end(week_start)
    else:
        total_spent      = 0.0
        week_start       = None
        week_end         = None

    weekly_remaining = round(weekly_budget - total_spent, 2)
    weekly_exceeded  = weekly_budget > 0.0 and total_spent > weekly_budget

    return HomeBudgetSummary(
        weekly_budget    = weekly_budget,
        daily_limit      = daily_limit,
        monthly_budget   = monthly_budget,
        total_spent      = total_spent,
        weekly_remaining = weekly_remaining,
        weekly_exceeded  = weekly_exceeded,
        week_start       = week_start,
        week_end         = week_end,
    )


async def _fetch_expenses(user_id: int) -> HomeExpenseSummary:
    """Own session — safe to run concurrently."""
    today = date.today()

    async with AsyncSessionLocal() as db:
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


async def _fetch_goal(user_id: int) -> Optional[HomeGoalSummary]:
    """Own session — safe to run concurrently."""
    completion_ratio = case(
        (Goal.target_amount > 0, Goal.saved_amount / Goal.target_amount),
        else_=0,
    )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Goal)
            .where(
                Goal.user_id      == user_id,
                Goal.is_completed == False,   # noqa: E712
            )
            .order_by(completion_ratio.desc())
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
    summary="Home screen — budget limits from Settings, live totals from BudgetActive",
)
async def get_home_route(
    current_user: dict = Depends(get_current_user),
) -> HomeResponse:
    user_id = current_user["id"]

    budget, expenses, goal = await asyncio.gather(
        _fetch_budget(user_id),
        _fetch_expenses(user_id),
        _fetch_goal(user_id),
    )

    logger.info(
        f"Home loaded: user_id={user_id} "
        f"spent_today={expenses.spent_today} "
        f"weekly_budget={budget.weekly_budget} "
        f"daily_limit={budget.daily_limit} "
        f"active_goal={'yes' if goal else 'none'}"
    )

    return HomeResponse(budget=budget, expenses=expenses, goal=goal)
