"""
analytics_route.py
──────────────────
All endpoints for the AnalyticsScreen.kt.

Endpoints
─────────
GET /analytics/summary?month=1&year=2026   → combined one-shot response
GET /analytics/total?month=1&year=2026     → total spent card only
GET /analytics/categories?month=1&year=2026→ category donut chart
GET /analytics/trend?months=6              → monthly line chart
GET /analytics/heatmap?month=1&year=2026   → spending calendar heatmap

Cache strategy (Upstash Redis):
    All analytics endpoints are cached per user+month+year.
    TTL = 5 minutes (analytics are read-heavy, write-light).
    Cache is invalidated automatically when any expense is added,
    edited, or deleted — call invalidate_analytics_cache() from
    expense_crud.py after each mutation (see note at bottom).
"""

import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.auth import get_current_user
from db import get_db
from crud.analytics_crud import (
    get_analytics_summary,
    get_category_breakdown,
    get_heatmap,
    get_monthly_trend,
    get_summary,
)
from schemas.analytics_schema import (
    AnalyticsSummaryResponse,
    CategoryBreakdownResponse,
    HeatmapResponse,
    MonthlyTrendResponse,
    TotalSpentResponse,
)

# ── Optional Redis cache (Upstash) ─────────────────────────────────────────────
# If UPSTASH_REDIS_REST_URL is not set, caching is silently skipped.
# Install: pip install upstash-redis
try:
    from upstash_redis import Redis
    _redis = Redis.from_env()
    _CACHE_ENABLED = True
except Exception:
    _redis = None
    _CACHE_ENABLED = False

_CACHE_TTL = 300   # 5 minutes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", tags=["analytics"])


# ══════════════════════════════════════════════════════════════════════════════
# Cache helpers
# ══════════════════════════════════════════════════════════════════════════════

def _cache_key(user_id: int, endpoint: str, month: int, year: int) -> str:
    return f"analytics:{user_id}:{endpoint}:{year}:{month}"


def _trend_key(user_id: int, months: int) -> str:
    return f"analytics:{user_id}:trend:{months}"


async def _get_cache(key: str):
    if not _CACHE_ENABLED:
        return None
    try:
        value = _redis.get(key)
        return json.loads(value) if value else None
    except Exception as e:
        logger.warning(f"Cache GET failed: {e}")
        return None


async def _set_cache(key: str, value: dict) -> None:
    if not _CACHE_ENABLED:
        return
    try:
        _redis.set(key, json.dumps(value), ex=_CACHE_TTL)
    except Exception as e:
        logger.warning(f"Cache SET failed: {e}")


async def invalidate_analytics_cache(user_id: int) -> None:
    """
    Call this from expense_crud.py after any add / edit / delete.
    Clears all analytics keys for this user so stale data is never shown.

    Usage in expense_crud.py:
        from routes.analytics_route import invalidate_analytics_cache
        await invalidate_analytics_cache(user_id)
    """
    if not _CACHE_ENABLED:
        return
    try:
        keys = _redis.keys(f"analytics:{user_id}:*")
        if keys:
            _redis.delete(*keys)
            logger.info(f"Analytics cache invalidated for user_id={user_id} ({len(keys)} keys)")
    except Exception as e:
        logger.warning(f"Cache invalidation failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint 1 — Combined summary (recommended: one call loads the full screen)
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/summary",
    response_model=AnalyticsSummaryResponse,
    summary="All analytics in one call — powers the full AnalyticsScreen",
    description="""
Returns all four analytics sections in a single response so
AnalyticsScreen.kt can load everything in one network call.

- `month` defaults to the current month (1–12)
- `year`  defaults to the current year
- `trend_months` controls how many months the line chart shows (default 6)

All sections are cached per user per month — invalidated automatically
when any expense is added, edited, or deleted.

Example response:
```json
{
    "summary": {
        "total_spent": 2450.0,
        "monthly_budget": 3000.0,
        "used_pct": 81.67,
        "remaining": 550.0,
        "savings_rate": 18.33
    },
    "category_breakdown": {
        "total": 2450.0,
        "categories": [
            { "category": "Housing",  "amount": 980.0,  "percentage": 40.0 },
            { "category": "Food",     "amount": 490.0,  "percentage": 20.0 },
            { "category": "Transport","amount": 367.5,  "percentage": 15.0 },
            { "category": "Utilities","amount": 245.0,  "percentage": 10.0 },
            { "category": "Leisure",  "amount": 367.5,  "percentage": 15.0 }
        ]
    },
    "monthly_trend": {
        "trend_pct": 12.5,
        "months": [
            { "month": "Aug", "year": 2025, "amount": 1800.0 },
            { "month": "Sep", "year": 2025, "amount": 2100.0 },
            { "month": "Oct", "year": 2025, "amount": 1600.0 },
            { "month": "Nov", "year": 2025, "amount": 2400.0 },
            { "month": "Dec", "year": 2025, "amount": 2000.0 },
            { "month": "Jan", "year": 2026, "amount": 2450.0 }
        ]
    },
    "heatmap": {
        "year": 2026,
        "month": 1,
        "avg_spend": 79.0,
        "daily": { "1": 20.0, "5": 210.0, "12": 190.0, "20": 175.0 }
    }
}
```
""",
)
async def get_summary_route(
    month        : int          = Query(default=None, ge=1, le=12, description="Month 1–12 (default: current month)"),
    year         : int          = Query(default=None, ge=2020,     description="Year (default: current year)"),
    trend_months : int          = Query(default=6,    ge=3, le=12,  description="How many months for the trend line"),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    today   = date.today()
    month   = month or today.month
    year    = year  or today.year
    user_id = current_user["id"]

    key    = _cache_key(user_id, "summary", month, year)
    cached = await _get_cache(key)
    if cached:
        return AnalyticsSummaryResponse(**cached)

    result = await get_analytics_summary(db, user_id, month, year, trend_months)
    await _set_cache(key, result.model_dump())
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint 2 — Total spent card only
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/total",
    response_model=TotalSpentResponse,
    summary="Total spent + budget for the month — TotalSpentCard",
)
async def get_total_route(
    month        : int          = Query(default=None, ge=1, le=12),
    year         : int          = Query(default=None, ge=2020),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    today   = date.today()
    month   = month or today.month
    year    = year  or today.year
    user_id = current_user["id"]

    key    = _cache_key(user_id, "total", month, year)
    cached = await _get_cache(key)
    if cached:
        return TotalSpentResponse(**cached)

    result = await get_summary(db, user_id, month, year)
    await _set_cache(key, result.model_dump())
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint 3 — Category breakdown
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/categories",
    response_model=CategoryBreakdownResponse,
    summary="Category breakdown — donut chart",
)
async def get_categories_route(
    month        : int          = Query(default=None, ge=1, le=12),
    year         : int          = Query(default=None, ge=2020),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    today   = date.today()
    month   = month or today.month
    year    = year  or today.year
    user_id = current_user["id"]

    key    = _cache_key(user_id, "categories", month, year)
    cached = await _get_cache(key)
    if cached:
        return CategoryBreakdownResponse(**cached)

    result = await get_category_breakdown(db, user_id, month, year)
    await _set_cache(key, result.model_dump())
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint 4 — Monthly trend
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/trend",
    response_model=MonthlyTrendResponse,
    summary="Monthly spend trend — line chart (last N months)",
)
async def get_trend_route(
    months       : int          = Query(default=6, ge=3, le=12, description="Number of months to return"),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    user_id = current_user["id"]

    key    = _trend_key(user_id, months)
    cached = await _get_cache(key)
    if cached:
        return MonthlyTrendResponse(**cached)

    result = await get_monthly_trend(db, user_id, months)
    await _set_cache(key, result.model_dump())
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint 5 — Spending heatmap
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/heatmap",
    response_model=HeatmapResponse,
    summary="Daily spend heatmap — calendar view",
)
async def get_heatmap_route(
    month        : int          = Query(default=None, ge=1, le=12),
    year         : int          = Query(default=None, ge=2020),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    today   = date.today()
    month   = month or today.month
    year    = year  or today.year
    user_id = current_user["id"]

    key    = _cache_key(user_id, "heatmap", month, year)
    cached = await _get_cache(key)
    if cached:
        return HeatmapResponse(**cached)

    result = await get_heatmap(db, user_id, month, year)
    await _set_cache(key, result.model_dump())
    return result


# ══════════════════════════════════════════════════════════════════════════════
# NOTE — Cache invalidation hook for expense_crud.py
# ══════════════════════════════════════════════════════════════════════════════
#
# Add this to the bottom of add_expense(), edit_expense(), delete_expense()
# in expense_crud.py:
#
#     from routes.analytics_route import invalidate_analytics_cache
#     await invalidate_analytics_cache(user_id)
#
# This ensures the analytics cache is cleared the moment the user
# adds, edits, or deletes any expense — no stale data ever shown.
