"""
analytics_crud.py
─────────────────
All async database operations for the analytics feature.

Same pattern as expense_crud.py — SQLAlchemy Core-style select with
await db.execute(...). No ORM lazy loading, all aggregations done in SQL.

Data sources:
    Expense  → total_spent, category_breakdown, monthly_trend, heatmap
    Settings → monthly_budget (for used_pct and savings_rate)
    Goal     → (not used here — goal progress is on GoalScreen itself)

Query index usage:
    summary          → ix_expenses_user_id  + date range filter
    category_breakdown→ ix_expenses_user_category
    monthly_trend    → ix_expenses_user_date  (GROUP BY month)
    heatmap          → ix_expenses_user_date  (GROUP BY day)
"""

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime, timezone

from sqlalchemy import cast, extract, func, select
from sqlalchemy import Date as SADate
from sqlalchemy.ext.asyncio import AsyncSession

from models.expense_model import Expense
from models.setting_model import Settings
from schemas.analytics_schema import (
    AnalyticsSummaryResponse,
    CategoryBreakdownResponse,
    CategoryItem,
    HeatmapResponse,
    MonthlyTrendResponse,
    MonthTrendItem,
    TotalSpentResponse,
)

logger = logging.getLogger(__name__)

# Short month labels for MonthTrendItem
_MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ══════════════════════════════════════════════════════════════════════════════
# 1. TOTAL SPENT SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

async def get_summary(
    db      : AsyncSession,
    user_id : int,
    month   : int,
    year    : int,
) -> TotalSpentResponse:
    """
    Total spent for a given month + budget comparison.

    - total_spent    → SUM(amount) for month/year
    - monthly_budget → pulled from Settings.budget_data JSON
    - used_pct       → total_spent / monthly_budget × 100
    - remaining      → monthly_budget − total_spent
    - savings_rate   → remaining / monthly_budget × 100 (0 if budget not set)
    """
    # ── Expense SUM ────────────────────────────────────────────────────
    result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0))
        .where(
            Expense.user_id == user_id,
            extract("month", Expense.date) == month,
            extract("year",  Expense.date) == year,
        )
    )
    total_spent = float(result.scalar_one())

    # ── Monthly budget from Settings JSON ──────────────────────────────
    monthly_budget = await _get_monthly_budget(db, user_id, year, month)

    used_pct      = round(total_spent / monthly_budget * 100, 2) if monthly_budget > 0 else 0.0
    remaining     = round(monthly_budget - total_spent, 2)
    savings_rate  = round(remaining / monthly_budget * 100, 2) if monthly_budget > 0 else 0.0

    return TotalSpentResponse(
        total_spent    = round(total_spent, 2),
        monthly_budget = monthly_budget,
        used_pct       = min(used_pct, 100.0),
        remaining      = remaining,
        savings_rate   = max(savings_rate, 0.0),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. CATEGORY BREAKDOWN
# ══════════════════════════════════════════════════════════════════════════════

async def get_category_breakdown(
    db      : AsyncSession,
    user_id : int,
    month   : int,
    year    : int,
) -> CategoryBreakdownResponse:
    """
    Spending grouped by category for a given month.
    Returns top 5 categories sorted by amount DESC.
    Uses ix_expenses_user_category.
    """
    result = await db.execute(
        select(
            Expense.category,
            func.sum(Expense.amount).label("total"),
        )
        .where(
            Expense.user_id == user_id,
            extract("month", Expense.date) == month,
            extract("year",  Expense.date) == year,
        )
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .limit(5)
    )
    rows = result.all()

    grand_total = sum(float(r.total) for r in rows)

    categories = [
        CategoryItem(
            category   = r.category,
            amount     = round(float(r.total), 2),
            percentage = round(float(r.total) / grand_total * 100, 2) if grand_total > 0 else 0.0,
        )
        for r in rows
    ]

    return CategoryBreakdownResponse(
        total      = round(grand_total, 2),
        categories = categories,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. MONTHLY TREND (last N months)
# ══════════════════════════════════════════════════════════════════════════════

async def get_monthly_trend(
    db      : AsyncSession,
    user_id : int,
    months  : int = 6,
) -> MonthlyTrendResponse:
    """
    Total spend grouped by month for the last N months (default 6).
    Months with zero spend are included as 0.0 so the line chart
    never has gaps.

    trend_pct = month-over-month change between the last two months.
    Uses ix_expenses_user_date.
    """
    today       = date.today()
    month_slots = _last_n_months(today.year, today.month, months)

    # Single query — pull all rows in the date window
    start_date = date(month_slots[0][1], month_slots[0][0], 1)
    result = await db.execute(
        select(
            extract("month", Expense.date).label("m"),
            extract("year",  Expense.date).label("y"),
            func.sum(Expense.amount).label("total"),
        )
        .where(
            Expense.user_id == user_id,
            cast(Expense.date, SADate) >= start_date,
        )
        .group_by("y", "m")
    )
    rows = {(int(r.y), int(r.m)): float(r.total) for r in result.all()}

    trend_months = [
        MonthTrendItem(
            month  = _MONTH_LABELS[m - 1],
            year   = y,
            amount = round(rows.get((y, m), 0.0), 2),
        )
        for m, y in month_slots
    ]

    # Month-over-month % change
    if len(trend_months) >= 2:
        last = trend_months[-1].amount
        prev = trend_months[-2].amount
        trend_pct = round((last - prev) / prev * 100, 2) if prev > 0 else 0.0
    else:
        trend_pct = 0.0

    return MonthlyTrendResponse(trend_pct=trend_pct, months=trend_months)


# ══════════════════════════════════════════════════════════════════════════════
# 4. SPENDING HEATMAP
# ══════════════════════════════════════════════════════════════════════════════

async def get_heatmap(
    db      : AsyncSession,
    user_id : int,
    month   : int,
    year    : int,
) -> HeatmapResponse:
    """
    Daily spend totals for a given month.
    Returns a dict of { "day": amount } for every day that has spend.
    Days with zero spend are omitted (client treats missing key as 0).
    Uses ix_expenses_user_date.
    """
    result = await db.execute(
        select(
            extract("day", Expense.date).label("day"),
            func.sum(Expense.amount).label("total"),
        )
        .where(
            Expense.user_id == user_id,
            extract("month", Expense.date) == month,
            extract("year",  Expense.date) == year,
        )
        .group_by("day")
        .order_by("day")
    )
    rows = result.all()

    daily     = {str(int(r.day)): round(float(r.total), 2) for r in rows}
    avg_spend = round(sum(daily.values()) / len(daily), 2) if daily else 0.0

    return HeatmapResponse(
        year      = year,
        month     = month,
        avg_spend = avg_spend,
        daily     = daily,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. COMBINED — single DB round trip via asyncio.gather in the route
# ══════════════════════════════════════════════════════════════════════════════

async def get_analytics_summary(
    db      : AsyncSession,
    user_id : int,
    month   : int,
    year    : int,
    trend_months: int = 6,
) -> AnalyticsSummaryResponse:
    """
    Calls all four analytics queries and returns them as one combined object.
    The route calls this with a single DB session so all four queries share
    the same transaction — consistent snapshot of data.
    """
    summary    = await get_summary(db, user_id, month, year)
    categories = await get_category_breakdown(db, user_id, month, year)
    trend      = await get_monthly_trend(db, user_id, trend_months)
    heatmap    = await get_heatmap(db, user_id, month, year)

    logger.info(
        f"Analytics loaded: user_id={user_id} "
        f"{month}/{year} spent={summary.total_spent}"
    )

    return AnalyticsSummaryResponse(
        summary            = summary,
        category_breakdown = categories,
        monthly_trend      = trend,
        heatmap            = heatmap,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _get_monthly_budget(
    db      : AsyncSession,
    user_id : int,
    year    : int,
    month   : int,
) -> float:
    """
    Read monthly_budget from Settings.budget_data JSON for the given month.
    Returns 0.0 if settings don't exist or month has no entry.
    """
    result = await db.execute(
        select(Settings.budget_data).where(Settings.user_id == user_id)
    )
    budget_data = result.scalar_one_or_none()
    if not budget_data:
        return 0.0

    month_key = _MONTH_LABELS[month - 1].lower()   # "jan", "feb", …
    year_key  = str(year)

    entry = budget_data.get(year_key, {}).get(month_key, {})
    return float(entry.get("monthly_budget", 0.0))


def _last_n_months(year: int, month: int, n: int) -> list[tuple[int, int]]:
    """
    Return a list of (month, year) tuples for the last N months
    in chronological order, ending at (month, year).

    Example: _last_n_months(2026, 1, 6) →
        [(8, 2025), (9, 2025), (10, 2025), (11, 2025), (12, 2025), (1, 2026)]
    """
    slots = []
    m, y  = month, year
    for _ in range(n):
        slots.append((m, y))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(slots))
