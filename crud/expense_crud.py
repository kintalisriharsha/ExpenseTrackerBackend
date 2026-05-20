"""
expense_crud.py
───────────────
All async database operations for the expense feature.

All queries use SQLAlchemy Core-style `select / update / delete` with
`await db.execute(...)` — same pattern as setting_crud.py.

Helper _to_response():
    Converts a raw Expense ORM row → ExpenseResponse, pre-formatting
    `date` ("20 May 2026") and `time` ("01:30 PM") from the stored
    UTC DateTime so Android can render them without any client-side work.

Query notes:
    - get_all_expenses  → ix_expenses_user_id  + ix_expenses_created_at
    - get_today_expenses→ ix_expenses_user_date
    - search_expenses   → ix_expenses_user_id  (+ optional category index)
    - add / edit / delete are PK lookups — always fast
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.expense_model import Expense
from schemas.expense_schema import ExpenseCreate, ExpenseUpdate, ExpenseResponse

logger = logging.getLogger(__name__)


# ── Private helper ─────────────────────────────────────────────────────────────

def _to_response(expense: Expense) -> ExpenseResponse:
    """
    Map ORM row → ExpenseResponse.
    Pre-formats date/time strings for direct display in Android UI.
    """
    dt: datetime = expense.date

    # Ensure we work in UTC even if the DB driver strips tzinfo
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # Windows does not support %-d, so build the day manually.
    date_str = f"{dt.day} {dt.strftime('%b %Y')}"

    return ExpenseResponse(
        id             = expense.id,
        user_id        = expense.user_id,
        amount         = float(expense.amount),
        category       = expense.category,
        notes          = expense.notes,
        date           = date_str,                    # "20 May 2026"
        time           = dt.strftime("%I:%M %p"),     # "01:30 PM"
        contact_name   = expense.contact_name,
        contact_number = expense.contact_number,
        created_at     = expense.created_at,
        updated_at     = expense.updated_at,
    )


# ── Fetch helpers ──────────────────────────────────────────────────────────────

async def _get_expense_by_id(
    db         : AsyncSession,
    expense_id : int,
    user_id    : int,
) -> Expense:
    """
    Fetch a single expense row that belongs to the given user.
    Raises 404 if not found (also prevents one user reading another's data).
    """
    result = await db.execute(
        select(Expense).where(
            Expense.id      == expense_id,
            Expense.user_id == user_id,
        )
    )
    expense = result.scalars().first()
    if not expense:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Expense id={expense_id} not found.",
        )
    return expense


# ══════════════════════════════════════════════════════════════════════════════
# CREATE
# ══════════════════════════════════════════════════════════════════════════════

async def add_expense(
    db      : AsyncSession,
    user_id : int,
    payload : ExpenseCreate,
) -> ExpenseResponse:
    """
    Insert a new expense row.
    Called when the user taps 'Add Expense' in AddExpense.kt.
    """
    expense = Expense(
        user_id        = user_id,
        amount         = payload.amount,
        category       = payload.category,
        notes          = payload.notes or "",
        date           = payload.date,
        contact_name   = payload.contact_name,
        contact_number = payload.contact_number,
    )
    db.add(expense)
    await db.flush()   # populate expense.id before returning
    await db.refresh(expense)

    logger.info(
        f"Expense added: id={expense.id} user_id={user_id} "
        f"amount={expense.amount} category={expense.category}"
    )
    return _to_response(expense)


# ══════════════════════════════════════════════════════════════════════════════
# READ — all expenses (limit 50, newest first)
# ══════════════════════════════════════════════════════════════════════════════

async def get_all_expenses(
    db      : AsyncSession,
    user_id : int,
    limit   : int = 50,
    offset  : int = 0,
) -> tuple[int, list[ExpenseResponse]]:
    """
    Return the most recent `limit` expenses for the user (default 50)
    plus the total count of all their expenses.

    Uses ix_expenses_user_id + ix_expenses_created_at.

    Returns (total_count, expense_list).
    """
    # Total count (no limit/offset)
    count_result = await db.execute(
        select(func.count()).where(Expense.user_id == user_id)
    )
    total = count_result.scalar_one()

    # Paginated rows — newest first
    result = await db.execute(
        select(Expense)
        .where(Expense.user_id == user_id)
        .order_by(Expense.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    expenses = result.scalars().all()

    return total, [_to_response(e) for e in expenses]


# ══════════════════════════════════════════════════════════════════════════════
# READ — today's expenses
# ══════════════════════════════════════════════════════════════════════════════

async def get_today_expenses(
    db      : AsyncSession,
    user_id : int,
) -> tuple[int, list[ExpenseResponse]]:
    """
    Return all expenses whose `date` falls on today (server UTC date).
    Used by HomeScreen's "Today's Expenses" section and DailySummaryCard.

    Uses ix_expenses_user_date.

    Returns (total_today_count, expense_list).
    """
    today = date.today()

    # Cast the stored timestamp to DATE for comparison
    from sqlalchemy import cast, Date as SADate

    result = await db.execute(
        select(Expense)
        .where(
            Expense.user_id == user_id,
            cast(Expense.date, SADate) == today,
        )
        .order_by(Expense.date.desc())
    )
    expenses = result.scalars().all()
    return len(expenses), [_to_response(e) for e in expenses]


# ══════════════════════════════════════════════════════════════════════════════
# READ — search / filter
# ══════════════════════════════════════════════════════════════════════════════

async def search_expenses(
    db       : AsyncSession,
    user_id  : int,
    query    : Optional[str] = None,
    category : Optional[str] = None,
    limit    : int           = 50,
    offset   : int           = 0,
) -> tuple[int, list[ExpenseResponse]]:
    """
    Full-text style search over notes + contact_name, with optional
    category filter.  Triggered from the search icon in HistoryScreen.kt
    and SearchDialog.

    - `query`    → case-insensitive substring match on notes OR contact_name
    - `category` → exact category filter (e.g. "Food")
    - Defaults to limit=50, same as get_all_expenses

    Uses ix_expenses_user_id (+ ix_expenses_user_category when category given).

    Returns (total_matched_count, expense_list).
    """
    base = select(Expense).where(Expense.user_id == user_id)

    if query:
        pattern = f"%{query}%"
        base = base.where(
            Expense.notes.ilike(pattern)
            | Expense.contact_name.ilike(pattern)
            | Expense.category.ilike(pattern)
        )

    if category:
        base = base.where(Expense.category == category.strip().title())

    # Count matched rows
    count_result = await db.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = count_result.scalar_one()

    # Paginated rows
    result = await db.execute(
        base.order_by(Expense.created_at.desc()).limit(limit).offset(offset)
    )
    expenses = result.scalars().all()

    return total, [_to_response(e) for e in expenses]


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE (PATCH)
# ══════════════════════════════════════════════════════════════════════════════

async def edit_expense(
    db         : AsyncSession,
    user_id    : int,
    expense_id : int,
    payload    : ExpenseUpdate,
) -> ExpenseResponse:
    """
    Partial update — only supplied fields are written (PATCH semantics).
    Triggered from the edit icon in DetailScreen.kt.

    Raises 404 if the expense doesn't exist or doesn't belong to the user.
    """
    expense = await _get_expense_by_id(db, expense_id, user_id)

    # Only mutate fields that were actually sent
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields supplied to update.",
        )

    for field, value in update_data.items():
        setattr(expense, field, value)

    await db.flush()
    await db.refresh(expense)

    logger.info(
        f"Expense updated: id={expense_id} user_id={user_id} fields={list(update_data)}"
    )
    return _to_response(expense)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE
# ══════════════════════════════════════════════════════════════════════════════

async def delete_expense(
    db         : AsyncSession,
    user_id    : int,
    expense_id : int,
) -> None:
    """
    Hard-delete a single expense.
    Triggered from the red 'Delete Transaction' button in DetailScreen.kt.

    Raises 404 if the expense doesn't exist or doesn't belong to the user.
    """
    # Verify ownership before deleting
    await _get_expense_by_id(db, expense_id, user_id)

    await db.execute(
        delete(Expense).where(
            Expense.id      == expense_id,
            Expense.user_id == user_id,
        )
    )
    logger.info(f"Expense deleted: id={expense_id} user_id={user_id}")