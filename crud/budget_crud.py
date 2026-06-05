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

Weekly budget source
────────────────────
weekly_budget is NO LONGER stored on BudgetActive.
It is read from Settings.budget_data (current month entry) on every
response build so there is a single source of truth.

Rollover (Monday WorkManager)
─────────────────────────────
1. Read budget_active for the user.
2. If week_start is already this Monday → already rolled over, return early.
3. Copy budget_active → INSERT into budget_history (idempotent via ON CONFLICT DO NOTHING).
4. Reset budget_active: clear tasks_data, reset total_spent = 0,
   set week_start = this Monday.
   weekly_budget is NOT carried forward here — it always comes from Settings.
"""

from __future__ import annotations

import copy
import logging
from datetime import date, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.budget_model import BudgetActive, BudgetHistory
from models.setting_model import Settings
from schemas.budget_schema import (
    AddTaskRequest,
    UpdateTaskRequest,
    TaskItem,
    ActiveWeekResponse,
    DayPlanResponse,
    TaskResponse,
    DeleteTaskResponse,
    HistoryWeekSummary,
    HistoryListResponse,
    RolloverResponse,
)

logger = logging.getLogger(__name__)

# ── MONTHS list (mirrors setting_crud) ────────────────────────────────────────

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]


# ── Private week helpers ───────────────────────────────────────────────────────

def _monday_of(d: date) -> date:
    """Return the Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def _sunday_of(monday: date) -> date:
    return monday + timedelta(days=6)


def _week_dates(monday: date) -> list[str]:
    """Return ISO date strings for all 7 days Mon–Sun."""
    return [(monday + timedelta(days=i)).isoformat() for i in range(7)]


def _empty_tasks_data(monday: date) -> dict:
    return {d: [] for d in _week_dates(monday)}


def _recalc_total(tasks_data: dict) -> float:
    total = 0.0
    for day_tasks in tasks_data.values():
        for task in day_tasks:
            total += float(task.get("budget", 0.0))
    return round(total, 2)


def _find_task(tasks_data: dict, task_id: str) -> tuple[str, int] | None:
    for day_key, day_tasks in tasks_data.items():
        for idx, task in enumerate(day_tasks):
            if task.get("id") == task_id:
                return day_key, idx
    return None


def _today_year_month() -> tuple[str, str]:
    today = date.today()
    return str(today.year), MONTHS[today.month - 1]


# ── Private: read weekly_budget from Settings ──────────────────────────────────

async def _get_weekly_budget_from_settings(
    db      : AsyncSession,
    user_id : int,
) -> float:
    """
    Read the current month's weekly_budget from Settings.budget_data.
    Returns 0.0 if Settings row doesn't exist or field is missing.
    This is the single source of truth for weekly_budget.
    """
    result = await db.execute(
        select(Settings).where(Settings.user_id == user_id)
    )
    settings = result.scalars().first()

    if not settings:
        return 0.0

    year_str, month_str = _today_year_month()
    month_entry = (
        settings.budget_data
        .get(year_str, {})
        .get(month_str, {})
    )
    return float(month_entry.get("weekly_budget", 0.0))


# ── Private: build response ────────────────────────────────────────────────────

def _build_active_response(
    active        : BudgetActive,
    weekly_budget : float,
) -> ActiveWeekResponse:
    """Convert a BudgetActive ORM row → ActiveWeekResponse.
    weekly_budget is injected from Settings — not read from BudgetActive.
    """
    monday   = active.week_start
    sunday   = _sunday_of(monday)
    spent    = float(active.total_spent)
    exceeded = weekly_budget > 0.0 and spent > weekly_budget

    days = []
    for day_str in _week_dates(monday):
        raw_tasks = active.tasks_data.get(day_str, [])
        tasks = [TaskItem(**t) for t in raw_tasks]
        days.append(DayPlanResponse(day_date=date.fromisoformat(day_str), tasks=tasks))

    return ActiveWeekResponse(
        user_id         = active.user_id,
        week_start      = monday,
        week_end        = sunday,
        weekly_budget   = weekly_budget,
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
    Note: weekly_budget is no longer a column — not set here.
    """
    active = await _get_active(db, user_id)
    if active:
        return active

    monday = _monday_of(date.today())
    active = BudgetActive(
        user_id     = user_id,
        week_start  = monday,
        total_spent = 0.0,
        tasks_data  = _empty_tasks_data(monday),
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
    weekly_budget is read from Settings — single source of truth.
    Auto-creates the BudgetActive row if needed.
    """
    active        = await _get_active_or_create(db, user_id)
    weekly_budget = await _get_weekly_budget_from_settings(db, user_id)
    return _build_active_response(active, weekly_budget)


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
    weekly_budget is read from Settings for the exceeded check.
    """
    active        = await _get_active_or_create(db, user_id)
    weekly_budget = await _get_weekly_budget_from_settings(db, user_id)
    day_str       = payload.day_date.isoformat()

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

    # Weekly budget exceeded check
    new_total = float(active.total_spent) + payload.budget
    if weekly_budget > 0.0 and new_total > weekly_budget:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Adding this task (₹{payload.budget}) would exceed the weekly "
                f"budget (₹{weekly_budget}). Current spent: ₹{active.total_spent}."
            ),
        )

    # Mutate JSON
    new_data = copy.deepcopy(active.tasks_data)
    new_task = {
        "id"     : payload.id,
        "name"   : payload.name,
        "budget" : payload.budget,
        "is_done": False,
    }
    new_data.setdefault(day_str, []).append(new_task)

    active.tasks_data  = new_data
    active.total_spent = _recalc_total(new_data)
    await db.flush()

    new_spent    = float(active.total_spent)
    new_exceeded = weekly_budget > 0.0 and new_spent > weekly_budget

    logger.info(
        f"Task added: user_id={user_id} day={day_str} "
        f"id={payload.id!r} budget={payload.budget} total_spent={new_spent}"
    )

    return TaskResponse(
        task            = TaskItem(**new_task),
        day_date        = payload.day_date,
        total_spent     = new_spent,
        weekly_budget   = weekly_budget,
        weekly_exceeded = new_exceeded,
    )


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE — edit task
# ══════════════════════════════════════════════════════════════════════════════

async def update_task(
    db      : AsyncSession,
    user_id : int,
    task_id : str,
    payload : UpdateTaskRequest,
) -> TaskResponse:
    """
    Edit a task's name, budget, or is_done flag.
    weekly_budget is read from Settings for the exceeded check.
    """
    active        = await _get_active_or_create(db, user_id)
    weekly_budget = await _get_weekly_budget_from_settings(db, user_id)
    located       = _find_task(active.tasks_data, task_id)

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

    new_spent    = float(active.total_spent)
    new_exceeded = weekly_budget > 0.0 and new_spent > weekly_budget

    logger.info(
        f"Task updated: user_id={user_id} task_id={task_id!r} "
        f"fields={payload.model_dump(exclude_unset=True)}"
    )

    return TaskResponse(
        task            = TaskItem(**task),
        day_date        = date.fromisoformat(day_key),
        total_spent     = new_spent,
        weekly_budget   = weekly_budget,
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
    weekly_budget is read from Settings.
    """
    active        = await _get_active_or_create(db, user_id)
    weekly_budget = await _get_weekly_budget_from_settings(db, user_id)
    located       = _find_task(active.tasks_data, task_id)

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

    new_spent    = float(active.total_spent)
    new_exceeded = weekly_budget > 0.0 and new_spent > weekly_budget

    logger.info(
        f"Task deleted: user_id={user_id} task_id={task_id!r} "
        f"total_spent={new_spent}"
    )

    return DeleteTaskResponse(
        deleted_id      = task_id,
        total_spent     = new_spent,
        weekly_budget   = weekly_budget,
        weekly_exceeded = new_exceeded,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROLLOVER — Monday WorkManager job
# ══════════════════════════════════════════════════════════════════════════════

async def rollover_week(
    db                   : AsyncSession,
    user_id              : int,
    carry_forward_budget : bool,
) -> RolloverResponse:
    """
    Called by Android WorkManager every Monday morning.

    Steps:
    1. Check if active week_start is already this Monday → idempotent early return.
    2. Archive current active data → INSERT into budget_history.
    3. Reset budget_active for the new week.

    weekly_budget in the response is always read from Settings —
    carry_forward_budget flag is kept for API compatibility but no
    longer writes to BudgetActive (Settings is the source of truth).
    """
    this_monday   = _monday_of(date.today())
    active        = await _get_active_or_create(db, user_id)
    weekly_budget = await _get_weekly_budget_from_settings(db, user_id)

    # ── Idempotent check ──────────────────────────────────────────────
    if active.week_start == this_monday:
        logger.info(
            f"Rollover skipped — already on current week: "
            f"user_id={user_id} week_start={this_monday}"
        )
        return RolloverResponse(
            action                 = "already_rolled",
            archived_week_start    = None,
            new_week_start         = this_monday,
            carried_forward_budget = weekly_budget,
        )

    archived_week_start = active.week_start
    archived_week_end   = _sunday_of(archived_week_start)

    # ── Step 1: Archive current active week ───────────────────────────
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
            weekly_budget = weekly_budget,   # snapshot from Settings at rollover time
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
    active.week_start  = this_monday
    active.total_spent = 0.0
    active.tasks_data  = _empty_tasks_data(this_monday)
    await db.flush()

    logger.info(
        f"Rollover complete: user_id={user_id} "
        f"archived={archived_week_start} new={this_monday}"
    )

    return RolloverResponse(
        action                 = "rolled_over",
        archived_week_start    = archived_week_start,
        new_week_start         = this_monday,
        carried_forward_budget = weekly_budget,
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
    Return past weeks for the BudgetHistory tab. Newest week first.

    The current active week is always prepended as a live "in progress" entry
    (id=-1) so users can see this week without waiting for Monday's rollover.
    It is injected before pagination, so:
      - offset=0  → active week is item[0], then up to (limit-1) archived rows
      - offset≥1  → active week is excluded (it's already been scrolled past)

    weekly_budget in archived history rows is the snapshot taken at rollover time.
    The live active entry uses the current Settings value.
    """
    # ── Archived rows ─────────────────────────────────────────────────────────
    count_result = await db.execute(
        select(func.count()).where(BudgetHistory.user_id == user_id)
    )
    archived_total = count_result.scalar_one()

    # Adjust limit/offset for archived rows when the active week is prepended.
    # On page 0 the active entry occupies one slot, so we fetch limit-1 archived rows.
    archived_limit  = max(limit - 1, 0) if offset == 0 else limit
    archived_offset = max(offset - 1, 0) if offset > 0  else 0

    result = await db.execute(
        select(BudgetHistory)
        .where(BudgetHistory.user_id == user_id)
        .order_by(BudgetHistory.week_start.desc())
        .limit(archived_limit)
        .offset(archived_offset)
    )
    rows = result.scalars().all()

    archived_weeks = [
        HistoryWeekSummary(
            id            = row.id,
            week_start    = row.week_start,
            week_end      = row.week_end,
            weekly_budget = float(row.weekly_budget),
            total_spent   = float(row.total_spent),
            within_budget = (
                row.weekly_budget == 0.0 or
                float(row.total_spent) <= float(row.weekly_budget)
            ),
            tasks_data    = row.tasks_data,
            created_at    = row.created_at,
        )
        for row in rows
    ]

    # ── Active week (live, always first on page 0) ────────────────────────────
    # budget_active.week_start is always the current Monday after rollover,
    # so it can never clash with any budget_history row (which hold past weeks).
    # No duplicate check needed — they are always different weeks.
    weeks  = archived_weeks
    active = await _get_active(db, user_id)

    if active is not None and offset == 0:
        weekly_budget = await _get_weekly_budget_from_settings(db, user_id)
        monday        = active.week_start
        spent         = float(active.total_spent)
        active_entry  = HistoryWeekSummary(
            id            = -1,               # sentinel — not a real DB row
            week_start    = monday,
            week_end      = _sunday_of(monday),
            weekly_budget = weekly_budget,
            total_spent   = spent,
            within_budget = weekly_budget == 0.0 or spent <= weekly_budget,
            tasks_data    = active.tasks_data,
            created_at    = active.updated_at,
        )
        weeks = [active_entry] + archived_weeks

    # total: archived rows + 1 for the live active week
    total = archived_total + (1 if active is not None else 0)

    return HistoryListResponse(total=total, weeks=weeks)

async def rollover_all_users(
    db                   : AsyncSession,
    carry_forward_budget : bool = True
) -> dict:
    from sqlalchemy import select
    result   = await db.execute(select(BudgetActive.user_id))
    user_ids = result.scalars().all()

    results = []
    for user_id in user_ids:
        try:
            r = await rollover_week(db, user_id, carry_forward_budget)
            results.append({
                "user_id": user_id,
                "status" : "ok",
                "action" : r["action"]
            })
        except Exception as e:
            results.append({
                "user_id": user_id,
                "status" : "error",
                "detail" : str(e)
            })

    return {"processed": len(user_ids), "results": results}