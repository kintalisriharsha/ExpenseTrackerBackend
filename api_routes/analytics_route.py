"""
analytics_route.py
──────────────────
All endpoints for AnalyticsScreen.kt.

CACHING STRATEGY
────────────────
Every endpoint is cached separately by (user_id, month, year).
This means:
  - Switching months doesn't bust the current month's cache
  - Each month is independently cached and expires independently
  - cache_delete_all_analytics(user_id) busts all months at once
    via Redis SCAN — called from expense_route on any mutation

TTLs:
  - /summary, /total, /categories, /heatmap → TTL_ANALYTICS (5 min)
    These include today's data so they can't be too long.
  - /trend → TTL_ANALYTICS_TREND (30 min)
    Trend is a 6-month rolling view — only the current month tip changes.
    Longer TTL is safe here.
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from auth.auth import get_current_user
from db import get_db
from crud.analytics_crud import (
    get_analytics_summary, get_category_breakdown,
    get_heatmap, get_monthly_trend, get_summary,
)
from schemas.analytics_schema import (
    AnalyticsSummaryResponse, CategoryBreakdownResponse,
    HeatmapResponse, MonthlyTrendResponse, TotalSpentResponse,
)
from cache import (
    cache_get, cache_set,
    analytics_summary_key, analytics_total_key, analytics_categories_key,
    analytics_trend_key, analytics_heatmap_key,
    TTL_ANALYTICS, TTL_ANALYTICS_TREND,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", tags=["analytics"])


def _today_defaults(month, year):
    today = date.today()
    return month or today.month, year or today.year


# ══════════════════════════════════════════════════════════════════════════════
# 1. Combined summary  (most important — powers full AnalyticsScreen)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/summary", response_model=AnalyticsSummaryResponse,
            summary="All analytics in one call")
async def get_summary_route(
    month        : int          = Query(default=None, ge=1, le=12),
    year         : int          = Query(default=None, ge=2020),
    trend_months : int          = Query(default=6, ge=3, le=12),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    month, year = _today_defaults(month, year)
    user_id     = current_user["id"]
    key         = analytics_summary_key(user_id, month, year)

    cached = await cache_get(key)
    if cached:
        return cached

    data = await get_analytics_summary(db, user_id, month, year, trend_months)
    await cache_set(key, jsonable_encoder(data), TTL_ANALYTICS)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# 2. Total spent card
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/total", response_model=TotalSpentResponse,
            summary="Total spent + budget for the month")
async def get_total_route(
    month        : int          = Query(default=None, ge=1, le=12),
    year         : int          = Query(default=None, ge=2020),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    month, year = _today_defaults(month, year)
    user_id     = current_user["id"]
    key         = analytics_total_key(user_id, month, year)

    cached = await cache_get(key)
    if cached:
        return cached

    data = await get_summary(db, user_id, month, year)
    await cache_set(key, jsonable_encoder(data), TTL_ANALYTICS)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# 3. Category breakdown
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/categories", response_model=CategoryBreakdownResponse,
            summary="Category breakdown — donut chart")
async def get_categories_route(
    month        : int          = Query(default=None, ge=1, le=12),
    year         : int          = Query(default=None, ge=2020),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    month, year = _today_defaults(month, year)
    user_id     = current_user["id"]
    key         = analytics_categories_key(user_id, month, year)

    cached = await cache_get(key)
    if cached:
        return cached

    data = await get_category_breakdown(db, user_id, month, year)
    await cache_set(key, jsonable_encoder(data), TTL_ANALYTICS)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# 4. Monthly trend  (longer TTL — 30 min)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/trend", response_model=MonthlyTrendResponse,
            summary="Monthly spend trend — line chart")
async def get_trend_route(
    months       : int          = Query(default=6, ge=3, le=12),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    user_id = current_user["id"]
    key     = analytics_trend_key(user_id, months)

    cached = await cache_get(key)
    if cached:
        return cached

    data = await get_monthly_trend(db, user_id, months)
    await cache_set(key, jsonable_encoder(data), TTL_ANALYTICS_TREND)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# 5. Spending heatmap
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/heatmap", response_model=HeatmapResponse,
            summary="Daily spend heatmap — calendar view")
async def get_heatmap_route(
    month        : int          = Query(default=None, ge=1, le=12),
    year         : int          = Query(default=None, ge=2020),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    month, year = _today_defaults(month, year)
    user_id     = current_user["id"]
    key         = analytics_heatmap_key(user_id, month, year)

    cached = await cache_get(key)
    if cached:
        return cached

    data = await get_heatmap(db, user_id, month, year)
    await cache_set(key, jsonable_encoder(data), TTL_ANALYTICS)
    return data