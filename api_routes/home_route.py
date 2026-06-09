

"""
home_route.py
─────────────
Single aggregated endpoint powering HomeScreen.kt.

HomeResponse now contains 4 sections:
  budget   → Settings limits (daily_limit, weekly_budget, monthly_budget)
  expenses → today's expenses
  goal     → top active savings goal
  planner  → BudgetActive (task-based weekly planner totals + days)
             This is DISTINCT from budget — planner tracks planned tasks,
             budget tracks the user's personal spending limits.
"""

from __future__ import annotation

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import cast, case, select
from sqlalchemy import Date as SADate

from auth.auth import get_current_user
from db import AsyncSessionLocal
from models.budget_model import BudgetActive
from models.expense_model import Expense
from models.goal_model import Goal
from models.setting_model import Settings
from cache import cache_get, cache_set, home_key, TTL_HOME
from crud.budget_crud import (
    _get_active_or_create,
    _get_weekly_budget_from_settings,
    _build_active_response,
)
from schemas.budget_schema import ActiveWeekResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/home", tags=["home"])

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]


# ══════════════════════════════════════════════════════════════════════════════
# Response schemas
# ══════════════════════════════════════════════════════════════════════════════

class HomeBudgetSummary(BaseModel):
    """Personal budget limits from Settings — for alerts/tracking."""
    weekly_budget   : float
    daily_limit     : float
    monthly_budget  : float
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
    time     : str


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
    user_name : str
    budget    : HomeBudgetSummary       # Settings limits
    expenses  : HomeExpenseSummary      # today's expenses
    goal      : Optional[HomeGoalSummary]
    planner   : Optional[ActiveWeekResponse]  # Budget planner active week


# ══════════════════════════════════════════════════════════════════════════════
# Private helpers
# ══════════════════════════════════════════════════════════════════════════════

def _week_end(week_start: date) -> date:
    return week_start + timedelta(days=6)

def _calc_progress(saved: float, target: float) -> float:
    if target <= 0: return 0.0
    return round(min(saved / target * 100, 100.0), 2)

def _fmt_time(value) -> str:
    return value.strftime("%H:%M")

def _get_current_month_entry(budget_data: dict) -> dict:
    today     = date.today()
    year_str  = str(today.year)
    month_str = MONTHS[today.month - 1]
    return budget_data.get(year_str, {}).get(month_str, {})


# ══════════════════════════════════════════════════════════════════════════════
# Data fetchers — each opens its own session for asyncio.gather
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_budget(user_id: int) -> HomeBudgetSummary:
    """Settings limits + BudgetActive total_spent."""
    async with AsyncSessionLocal() as db:
        settings_result = await db.execute(
            select(Settings).where(Settings.user_id == user_id)
        )
        settings = settings_result.scalars().first()

        active_result = await db.execute(
            select(BudgetActive).where(BudgetActive.user_id == user_id)
        )
        active = active_result.scalars().first()

    if settings:
        entry          = _get_current_month_entry(settings.budget_data)
        weekly_budget  = float(entry.get("weekly_budget",  0.0))
        daily_limit    = float(entry.get("daily_limit",    0.0))
        monthly_budget = float(entry.get("monthly_budget", 0.0))
    else:
        weekly_budget = daily_limit = monthly_budget = 0.0

    if active:
        total_spent = float(active.total_spent)
        week_start  = active.week_start
        week_end    = _week_end(week_start)
    else:
        total_spent = 0.0
        week_start  = week_end = None

    weekly_remaining = round(weekly_budget - total_spent, 2)
    weekly_exceeded  = weekly_budget > 0.0 and total_spent > weekly_budget

    return HomeBudgetSummary(
        weekly_budget=weekly_budget, daily_limit=daily_limit,
        monthly_budget=monthly_budget, total_spent=total_spent,
        weekly_remaining=weekly_remaining, weekly_exceeded=weekly_exceeded,
        week_start=week_start, week_end=week_end,
    )


async def _fetch_expenses(user_id: int) -> HomeExpenseSummary:
    today = date.today()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Expense)
            .where(Expense.user_id == user_id, cast(Expense.date, SADate) == today)
            .order_by(Expense.date.desc())
        )
        all_today = result.scalars().all()

    spent_today = round(sum(float(e.amount) for e in all_today), 2)
    return HomeExpenseSummary(
        spent_today=spent_today,
        total_today=len(all_today),
        expenses=[
            HomeExpenseItem(
                id=e.id, notes=e.notes or "", category=e.category,
                amount=float(e.amount), time=_fmt_time(e.time),
            )
            for e in all_today[:3]
        ],
    )


async def _fetch_goal(user_id: int) -> Optional[HomeGoalSummary]:
    completion_ratio = case(
        (Goal.target_amount > 0, Goal.saved_amount / Goal.target_amount),
        else_=0,
    )
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Goal)
            .where(Goal.user_id == user_id, Goal.is_completed == False)  # noqa: E712
            .order_by(completion_ratio.desc())
            .limit(1)
        )
        goal = result.scalars().first()

    if not goal:
        return None
    saved  = float(goal.saved_amount)
    target = float(goal.target_amount)
    return HomeGoalSummary(
        id=goal.id, goal_name=goal.goal_name,
        target_amount=target, saved_amount=saved,
        progress_pct=_calc_progress(saved, target), category=goal.category,
    )


async def _fetch_planner(user_id: int) -> Optional[ActiveWeekResponse]:
    """
    Budget planner active week — task-based, completely separate from
    Settings budget limits. Returns None if user has no planner row yet.
    """
    async with AsyncSessionLocal() as db:
        active        = await _get_active_or_create(db, user_id)
        weekly_budget = await _get_weekly_budget_from_settings(db, user_id)
        await db.commit()   # flush the auto-create if it happened
        return _build_active_response(active, weekly_budget)


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "",
    response_model=HomeResponse,
    status_code=status.HTTP_200_OK,
    summary="Home screen — Settings limits, today's expenses, goal, and planner",
)
async def get_home_route(
    current_user: dict = Depends(get_current_user),
) -> HomeResponse:
    user_id   = current_user["id"]
    user_name = current_user["display_name"]
    key       = home_key(user_id)

    # ── Cache read ─────────────────────────────────────────────────────
    cached = await cache_get(key)
    if cached:
        logger.info(f"Home cache HIT: user_id={user_id}")
        return cached

    # ── Cache miss → 4 concurrent fetches ─────────────────────────────
    budget, expenses, goal, planner = await asyncio.gather(
        _fetch_budget(user_id),
        _fetch_expenses(user_id),
        _fetch_goal(user_id),
        _fetch_planner(user_id),
    )

    response = HomeResponse(
        user_name=user_name,
        budget=budget,
        expenses=expenses,
        goal=goal,
        planner=planner,
    )

    # ── Cache write ────────────────────────────────────────────────────
    await cache_set(key, jsonable_encoder(response), TTL_HOME)

    logger.info(
        f"Home loaded: user_id={user_id} "
        f"spent_today={expenses.spent_today} "
        f"planner_total={planner.total_spent if planner else 0}"
    )
    return response
