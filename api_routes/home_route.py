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
from db import get_db, AsyncSessionLocal          # ← import the sessionmaker
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
    weekly_budget   : float
    total_spent     : float
    remaining       : float
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
    from datetime import timedelta
    return week_start + timedelta(days=6)


def _calc_progress(saved: float, target: float) -> float:
    if target <= 0:
        return 0.0
    return round(min(saved / target * 100, 100.0), 2)


def _fmt_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%I:%M %p")


# ══════════════════════════════════════════════════════════════════════════════
# Data fetchers — each opens its OWN session so they can run concurrently
# via asyncio.gather without hitting SQLAlchemy's single-session restriction.
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_budget(user_id: int) -> HomeBudgetSummary:
    """Own session — safe to run concurrently."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BudgetActive).where(BudgetActive.user_id == user_id)
        )
        active = result.scalars().first()

    if not active:
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
    summary="Home screen — budget, today's expenses, and top savings goal in one call",
)
async def get_home_route(
    current_user : dict = Depends(get_current_user),
) -> HomeResponse:
    user_id = current_user["id"]

    # Each fetcher opens its own AsyncSession, so all three can run in
    # parallel without SQLAlchemy's single-session concurrency error.
    budget, expenses, goal = await asyncio.gather(
        _fetch_budget(user_id),
        _fetch_expenses(user_id),
        _fetch_goal(user_id),
    )

    logger.info(
        f"Home loaded: user_id={user_id} "
        f"spent_today={expenses.spent_today} "
        f"active_goal={'yes' if goal else 'none'}"
    )

    return HomeResponse(budget=budget, expenses=expenses, goal=goal)
