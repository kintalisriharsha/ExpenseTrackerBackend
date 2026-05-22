"""
budget_planner_crud.py
──────────────────────
All async database operations for the BudgetScreen.kt planner feature.

Pattern mirrors setting_crud.py:
    - Read JSON blob → mutate in Python → write back
    - Deep copy before mutate (required for SQLAlchemy JSON change detection)
    - total_spent kept in sync as a denormalized column on every write

Week helpers
────────────
_monday_of(date) → always returns the Monday of the week containing that date.
_week_dates(monday) → returns all 7 ISO date strings for Mon–Sun.
_recalc_total(tasks_data) → recomputes total_spent by summing all task budgets.

Task lookup
───────────
Tasks are stored inside a JSON dict keyed by ISO date string.
_find_task() scans all days to locate a task by its UUID — since a week
has at most ~20 tasks this is O(n) and fast enough without a secondary index.

Rollover (Monday WorkManager)
─────────────────────────────
1. Read budget_active for the user.
2. If week_start is already this Monday → already rolled over, return early.
3. Copy budget_active → INSERT into budget_history (idempotent via ON CONFLICT DO NOTHING).
4. Reset budget_active: clear tasks_data, reset total_spent = 0,
   set week_start = this Monday, optionally carry forward weekly_budget.
"""

from __future__ import annotations

import copy
import logging
import uuid
from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.budget_model import BudgetActive, BudgetHistory
from schemas.budget_schema import (
    AddTaskRequest,
    UpdateTaskRequest,
    TaskItem,
    ActiveWeekResponse,
    DayPlanResponse,
    TaskResponse,
    DeleteTaskResponse,
    WeeklyBudgetResponse,
    HistoryWeekSummary,
    HistoryListResponse,
    RolloverResponse,
)

logger = logging.getLogger(__name__)


# ── Private week helpers ───────────────────────────────────────────────────────

def _monday_of(d: date) -> date:
    """Return the Monday of the week containing d."""
    return d - timedelta(days=d.weekday())   # weekday(): Mon=0 … Sun=6


def _sunday_of(monday: date) -> date:
    """Return the Sunday of the week starting on monday."""
    return monday + timedelta(days=6)


def _week_dates(monday: date) -> list[str]:
    """Return ISO date strings for all 7 days Mon–Sun."""
    return [(monday + timedelta(days=i)).isoformat() for i in range(7)]


def _empty_tasks_data(monday: date) -> dict:
    """Return a tasks_data dict with empty lists for all 7 days."""
    return {d: [] for d in _week_dates(monday)}


def _recalc_total(tasks_data: dict) -> float:
    """Sum all task budgets across all days. Used to keep total_spent in sync."""
    total = 0.0
    for day_tasks in tasks_data.values():
        for task in day_tasks:
            total += float(task.get("budget", 0.0))
    return round(total, 2)


def _find_task(tasks_data: dict, task_id: str) -> tuple[str, int] | None:
    """
    Locate a task by its UUID across all days.
    Returns (day_key, list_index) or None if not found.
    """
    for day_key, day_tasks in tasks_data.items():
        for idx, task in enumerate(day_tasks):
            if task.get("id") == task_id:
                return day_key, idx
    return None


def _build_active_response(active: BudgetActive) -> ActiveWeekResponse:
    """Convert a BudgetActive ORM row → ActiveWeekResponse."""
    monday   = active.week_start
    sunday   = _sunday_of(monday)
    budget   = float(active.weekly_budget)
    spent    = float(active.total_spent)
    exceeded = budget > 0.0 and spent > budget

    days = []
    for day_str in _week_dates(monday):
        raw_tasks = active.tasks_data.get(day_str, [])
        tasks = [TaskItem(**t) for t in raw_tasks]
        days.append(DayPlanResponse(day_date=date.fromisoformat(day_str), tasks=tasks))

    return ActiveWeekResponse(
        user_id         = active.user_id,
        week_start      = monday,
        week_end        = sunday,
        weekly_budget   = budget,
        total_spent     = spent,
        weekly_exceeded = exceeded,
        days            = days,
        updated_at      = active.updated_at,
    )


# ── Fetch helpers ──────────────────────────────────────────────────────────────

async def _get_active(db: AsyncSession, user_id: int) -> BudgetActive | None:
    result = await db.execute(
        select(BudgetActive).where(BudgetActive.user_id == user_id)
    )
    return result.scalars().first()


async def _get_active_or_create(db: AsyncSession, user_id: int) -> BudgetActive:
    """
    Return the active week row, creating it if this user has never
    opened the Budget Planner before.
    """
    active = await _get_active(db, user_id)
    if active:
        return active

    monday = _monday_of(date.today())
    active = BudgetActive(
        user_id       = user_id,
        week_start    = monday,
        weekly_budget = 0.0,
        total_spent   = 0.0,
        tasks_data    = _empty_tasks_data(monday),
    )
    db.add(active)
    await db.flush()
    logger.info(f"BudgetActive created for user_id={user_id} week_start={monday}")
    return active


# ══════════════════════════════════════════════════════════════════════════════
# READ — current active week
# ══════════════════════════════════════════════════════════════════════════════

async def get_active_week(
    db      : AsyncSession,
    user_id : int,
) -> ActiveWeekResponse:
    """
    Return the current week's full plan.
    Auto-creates the row if this user has never opened BudgetScreen before.
    Called every time the BudgetScreen composable loads.
    """
    active = await _get_active_or_create(db, user_id)
    return _build_active_response(active)


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE — weekly budget cap
# ══════════════════════════════════════════════════════════════════════════════

async def set_weekly_budget(
    db            : AsyncSession,
    user_id       : int,
    weekly_budget : float,
) -> WeeklyBudgetResponse:
    """
    Set or update the weekly budget cap.
    Called when user confirms WeeklyBudgetDialog.
    """
    active = await _get_active_or_create(db, user_id)
    active.weekly_budget = weekly_budget
    await db.flush()

    spent    = float(active.total_spent)
    exceeded = weekly_budget > 0.0 and spent > weekly_budget

    logger.info(
        f"Weekly budget set: user_id={user_id} "
        f"weekly_budget={weekly_budget} total_spent={spent}"
    )

    return WeeklyBudgetResponse(
        weekly_budget   = weekly_budget,
        total_spent     = spent,
        weekly_exceeded = exceeded,
    )


# ══════════════════════════════════════════════════════════════════════════════
# CREATE — add task
# ══════════════════════════════════════════════════════════════════════════════

async def add_task(
    db      : AsyncSession,
    user_id : int,
    payload : AddTaskRequest,
) -> TaskResponse:
    """
    Add a task to a specific day in the active week.
    Called when user confirms AddTaskBottomSheet.

    Raises 400 if the day_date is outside the current active week.
    Raises 409 if task ID already exists (duplicate client UUID).
    Raises 403 if weekly budget is set and adding this task would exceed it
               and the user hasn't explicitly overridden (handled on Android side,
               but server validates too for safety).
    """
    active   = await _get_active_or_create(db, user_id)
    day_str  = payload.day_date.isoformat()

    # Validate day is within active week
    valid_days = _week_dates(active.week_start)
    if day_str not in valid_days:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"day_date {day_str} is not within the active week "
                   f"({valid_days[0]} – {valid_days[-1]}).",
        )

    # Duplicate task ID check
    if _find_task(active.tasks_data, payload.id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task id={payload.id!r} already exists in the active week.",
        )

    # Weekly budget enforcement
    budget = float(active.weekly_budget)
    spent  = float(active.total_spent)
    if budget > 0.0 and (spent + payload.budget) > budget:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Adding this task (₹{payload.budget:.2f}) would exceed the "
                f"weekly budget of ₹{budget:.2f}. "
                f"Currently spent: ₹{spent:.2f}, "
                f"remaining: ₹{max(budget - spent, 0):.2f}."
            ),
        )

    # Mutate JSON (deep copy required for SQLAlchemy change detection)
    new_data = copy.deepcopy(active.tasks_data)
    new_task = {
        "id":      payload.id,
        "name":    payload.name,
        "budget":  payload.budget,
        "is_done": False,
    }
    new_data.setdefault(day_str, []).append(new_task)

    active.tasks_data  = new_data
    active.total_spent = _recalc_total(new_data)
    await db.flush()

    new_spent    = float(active.total_spent)
    new_exceeded = budget > 0.0 and new_spent > budget

    logger.info(
        f"Task added: user_id={user_id} task_id={payload.id!r} "
        f"day={day_str} budget={payload.budget} total_spent={new_spent}"
    )

    return TaskResponse(
        task            = TaskItem(**new_task),
        day_date        = payload.day_date,
        total_spent     = new_spent,
        weekly_budget   = budget,
        weekly_exceeded = new_exceeded,
    )


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE — edit task (PATCH semantics)
# ══════════════════════════════════════════════════════════════════════════════

async def update_task(
    db      : AsyncSession,
    user_id : int,
    task_id : str,
    payload : UpdateTaskRequest,
) -> TaskResponse:
    """
    Edit a task's name, budget, or is_done flag.
    Called from EditTaskBottomSheet or checkbox toggle.
    PATCH semantics — only supplied fields are updated.

    Raises 404 if task not found in the active week.
    """
    active  = await _get_active_or_create(db, user_id)
    located = _find_task(active.tasks_data, task_id)

    if located is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task id={task_id!r} not found in the active week.",
        )

    day_key, idx = located

    # Mutate JSON
    new_data = copy.deepcopy(active.tasks_data)
    task     = new_data[day_key][idx]

    if payload.name    is not None: task["name"]    = payload.name
    if payload.budget  is not None: task["budget"]  = payload.budget
    if payload.is_done is not None: task["is_done"] = payload.is_done

    active.tasks_data  = new_data
    active.total_spent = _recalc_total(new_data)
    await db.flush()

    budget       = float(active.weekly_budget)
    new_spent    = float(active.total_spent)
    new_exceeded = budget > 0.0 and new_spent > budget

    logger.info(
        f"Task updated: user_id={user_id} task_id={task_id!r} "
        f"fields={payload.model_dump(exclude_unset=True)}"
    )

    return TaskResponse(
        task            = TaskItem(**task),
        day_date        = date.fromisoformat(day_key),
        total_spent     = new_spent,
        weekly_budget   = budget,
        weekly_exceeded = new_exceeded,
    )


# ══════════════════════════════════════════════════════════════════════════════
# DELETE — remove task
# ══════════════════════════════════════════════════════════════════════════════

async def delete_task(
    db      : AsyncSession,
    user_id : int,
    task_id : str,
) -> DeleteTaskResponse:
    """
    Remove a task from the active week.
    Called from the delete icon in TaskRow.
    Raises 404 if task not found.
    """
    active  = await _get_active_or_create(db, user_id)
    located = _find_task(active.tasks_data, task_id)

    if located is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task id={task_id!r} not found in the active week.",
        )

    day_key, idx = located

    new_data = copy.deepcopy(active.tasks_data)
    new_data[day_key].pop(idx)

    active.tasks_data  = new_data
    active.total_spent = _recalc_total(new_data)
    await db.flush()

    budget       = float(active.weekly_budget)
    new_spent    = float(active.total_spent)
    new_exceeded = budget > 0.0 and new_spent > budget

    logger.info(
        f"Task deleted: user_id={user_id} task_id={task_id!r} "
        f"total_spent={new_spent}"
    )

    return DeleteTaskResponse(
        deleted_id      = task_id,
        total_spent     = new_spent,
        weekly_budget   = budget,
        weekly_exceeded = new_exceeded,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROLLOVER — Monday WorkManager job
# ══════════════════════════════════════════════════════════════════════════════

async def rollover_week(
    db                    : AsyncSession,
    user_id               : int,
    carry_forward_budget  : bool,
) -> RolloverResponse:
    """
    Called by Android WorkManager every Monday morning.

    Steps:
    1. Check if active week_start is already this Monday → idempotent early return.
    2. Archive current active data → INSERT into budget_history.
       Uses INSERT ... ON CONFLICT DO NOTHING so double-firing is safe.
    3. Reset budget_active for the new week:
       - week_start = this Monday
       - tasks_data = empty 7-day structure
       - total_spent = 0
       - weekly_budget = carried forward or reset to 0
    """
    this_monday = _monday_of(date.today())
    active      = await _get_active_or_create(db, user_id)

    # ── Idempotent check ──────────────────────────────────────────────
    if active.week_start == this_monday:
        logger.info(
            f"Rollover skipped — already on current week: "
            f"user_id={user_id} week_start={this_monday}"
        )
        return RolloverResponse(
            action                  = "already_rolled",
            archived_week_start     = None,
            new_week_start          = this_monday,
            carried_forward_budget  = float(active.weekly_budget),
        )

    archived_week_start = active.week_start
    archived_week_end   = _sunday_of(archived_week_start)

    # ── Step 1: Archive current active week ───────────────────────────
    # Check for existing history row first (idempotent safety)
    existing_history = await db.execute(
        select(BudgetHistory).where(
            BudgetHistory.user_id    == user_id,
            BudgetHistory.week_start == archived_week_start,
        )
    )
    if existing_history.scalars().first() is None:
        history_row = BudgetHistory(
            user_id       = user_id,
            week_start    = archived_week_start,
            week_end      = archived_week_end,
            weekly_budget = active.weekly_budget,
            total_spent   = active.total_spent,
            tasks_data    = copy.deepcopy(active.tasks_data),
        )
        db.add(history_row)
        await db.flush()
        logger.info(
            f"Archived week: user_id={user_id} "
            f"week_start={archived_week_start} "
            f"total_spent={active.total_spent}"
        )

    # ── Step 2: Reset active for new week ─────────────────────────────
    new_budget = float(active.weekly_budget) if carry_forward_budget else 0.0

    active.week_start    = this_monday
    active.weekly_budget = new_budget
    active.total_spent   = 0.0
    active.tasks_data    = _empty_tasks_data(this_monday)
    await db.flush()

    logger.info(
        f"Rollover complete: user_id={user_id} "
        f"archived={archived_week_start} new={this_monday} "
        f"budget={new_budget} carry_forward={carry_forward_budget}"
    )

    return RolloverResponse(
        action                  = "rolled_over",
        archived_week_start     = archived_week_start,
        new_week_start          = this_monday,
        carried_forward_budget  = new_budget,
    )


# ══════════════════════════════════════════════════════════════════════════════
# READ — history list
# ══════════════════════════════════════════════════════════════════════════════

async def get_history(
    db      : AsyncSession,
    user_id : int,
    limit   : int = 10,
    offset  : int = 0,
) -> HistoryListResponse:
    """
    Return past weeks for the BudgetHistory tab.
    Newest week first. tasks_data included for drill-down on tap.
    """
    # Total count
    count_result = await db.execute(
        select(func.count()).where(BudgetHistory.user_id == user_id)
    )
    total = count_result.scalar_one()

    # Paginated rows — newest first
    result = await db.execute(
        select(BudgetHistory)
        .where(BudgetHistory.user_id == user_id)
        .order_by(BudgetHistory.week_start.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.scalars().all()

    weeks = [
        HistoryWeekSummary(
            id             = row.id,
            week_start     = row.week_start,
            week_end       = row.week_end,
            weekly_budget  = float(row.weekly_budget),
            total_spent    = float(row.total_spent),
            within_budget  = (
                row.weekly_budget == 0.0 or
                float(row.total_spent) <= float(row.weekly_budget)
            ),
            tasks_data     = row.tasks_data,
            created_at     = row.created_at,
        )
        for row in rows
    ]

    return HistoryListResponse(total=total, weeks=weeks)