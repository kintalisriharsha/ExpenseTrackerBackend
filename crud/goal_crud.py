"""
goal_crud.py
────────────
All async database operations for the goal feature.

Same pattern as expense_crud.py — SQLAlchemy Core-style select/update/delete
with await db.execute(...).

Helper _to_response():
    Converts a Goal ORM row → GoalResponse, computing progress_pct from
    saved_amount / target_amount so the Android UI can use it directly.

Auto-completion:
    Whenever saved_amount >= target_amount the backend flips is_completed=True
    automatically — the Android UI never needs to manage this flag.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.goal_model import Goal
from schemas.goal_schema import GoalCreate, GoalUpdate, GoalResponse

logger = logging.getLogger(__name__)


# ── Private helpers ────────────────────────────────────────────────────────────

def _calc_progress(saved: float, target: float) -> float:
    """Return progress as 0.0–100.0, guarded against division-by-zero."""
    if target <= 0:
        return 0.0
    return round(min(saved / target * 100, 100.0), 2)


def _to_response(goal: Goal) -> GoalResponse:
    """Map ORM row → GoalResponse with pre-computed progress_pct."""
    saved  = float(goal.saved_amount)
    target = float(goal.target_amount)
    return GoalResponse(
        id            = goal.id,
        user_id       = goal.user_id,
        goal_name     = goal.goal_name,
        target_amount = target,
        saved_amount  = saved,
        progress_pct  = _calc_progress(saved, target),
        category      = goal.category,
        is_completed  = goal.is_completed,
        created_at    = goal.created_at,
        updated_at    = goal.updated_at,
    )


async def _get_goal_by_id(
    db      : AsyncSession,
    goal_id : int,
    user_id : int,
) -> Goal:
    """
    Fetch a single goal that belongs to the given user.
    Raises 404 if not found (also prevents cross-user data access).
    """
    result = await db.execute(
        select(Goal).where(
            Goal.id      == goal_id,
            Goal.user_id == user_id,
        )
    )
    goal = result.scalars().first()
    if not goal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Goal id={goal_id} not found.",
        )
    return goal


# ══════════════════════════════════════════════════════════════════════════════
# CREATE
# ══════════════════════════════════════════════════════════════════════════════

async def add_goal(
    db      : AsyncSession,
    user_id : int,
    payload : GoalCreate,
) -> GoalResponse:
    """
    Insert a new goal row.
    Called when the user taps 'Create Goal' in AddGoal (SetGoal.kt).
    Auto-marks as completed if saved_amount already meets target (edge case).
    """
    saved  = payload.saved_amount or 0.0
    target = payload.target_amount
    completed = saved >= target

    goal = Goal(
        user_id       = user_id,
        goal_name     = payload.goal_name,
        target_amount = target,
        saved_amount  = saved,
        category      = payload.category,
        is_completed  = completed,
    )
    db.add(goal)
    await db.flush()
    await db.refresh(goal)

    logger.info(
        f"Goal added: id={goal.id} user_id={user_id} "
        f"name={goal.goal_name!r} target={target}"
    )
    return _to_response(goal)


# ══════════════════════════════════════════════════════════════════════════════
# READ — all goals (newest first, with pagination)
# ══════════════════════════════════════════════════════════════════════════════

async def get_all_goals(
    db      : AsyncSession,
    user_id : int,
    limit   : int = 50,
    offset  : int = 0,
) -> tuple[int, list[GoalResponse]]:
    """
    Return all goals for the user, newest first.
    Uses ix_goals_user_id + ix_goals_created_at.

    Returns (total_count, goal_list).
    """
    count_result = await db.execute(
        select(func.count()).where(Goal.user_id == user_id)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Goal)
        .where(Goal.user_id == user_id)
        .order_by(Goal.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    goals = result.scalars().all()

    return total, [_to_response(g) for g in goals]


# ══════════════════════════════════════════════════════════════════════════════
# READ — get by limit (used for home screen / dashboard widgets)
# ══════════════════════════════════════════════════════════════════════════════

async def get_goals_by_limit(
    db      : AsyncSession,
    user_id : int,
    limit   : int,
) -> tuple[int, list[GoalResponse]]:
    """
    Return the N most recent (active / incomplete) goals.
    Used by HomeScreen's SavingsBanner to show the top goal quickly.

    Active goals are prioritised (is_completed=False first), then
    sorted by created_at DESC.

    Returns (total_active_count, goal_list).
    """
    count_result = await db.execute(
        select(func.count()).where(
            Goal.user_id     == user_id,
            Goal.is_completed == False,   # noqa: E712 — SQLAlchemy requires ==
        )
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Goal)
        .where(
            Goal.user_id     == user_id,
            Goal.is_completed == False,
        )
        .order_by(Goal.created_at.desc())
        .limit(limit)
    )
    goals = result.scalars().all()

    return total, [_to_response(g) for g in goals]


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE (PATCH)
# ══════════════════════════════════════════════════════════════════════════════

async def edit_goal(
    db      : AsyncSession,
    user_id : int,
    goal_id : int,
    payload : GoalUpdate,
) -> GoalResponse:
    """
    Partial update — only supplied fields are written (PATCH semantics).

    Auto-completion logic:
        After any update, if saved_amount >= target_amount the backend
        sets is_completed=True automatically (unless the caller explicitly
        sets is_completed=False to re-open a goal).

    Raises 404 if the goal doesn't exist or doesn't belong to the user.
    Raises 422 if no fields are supplied.
    """
    goal = await _get_goal_by_id(db, goal_id, user_id)

    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields supplied to update.",
        )

    for field, value in update_data.items():
        setattr(goal, field, value)

    # Auto-complete check (only if caller didn't explicitly set is_completed)
    if "is_completed" not in update_data:
        saved  = float(goal.saved_amount)
        target = float(goal.target_amount)
        goal.is_completed = saved >= target

    await db.flush()
    await db.refresh(goal)

    logger.info(
        f"Goal updated: id={goal_id} user_id={user_id} fields={list(update_data)}"
    )
    return _to_response(goal)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE
# ══════════════════════════════════════════════════════════════════════════════

async def delete_goal(
    db      : AsyncSession,
    user_id : int,
    goal_id : int,
) -> None:
    """
    Hard-delete a single goal.
    Raises 404 if the goal doesn't exist or doesn't belong to the user.
    """
    await _get_goal_by_id(db, goal_id, user_id)   # ownership check

    await db.execute(
        delete(Goal).where(
            Goal.id      == goal_id,
            Goal.user_id == user_id,
        )
    )
    logger.info(f"Goal deleted: id={goal_id} user_id={user_id}")