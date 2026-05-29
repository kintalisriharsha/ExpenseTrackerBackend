"""
analytics_schema.py
───────────────────
Pydantic schemas for the analytics feature.

Matches the exact response shapes expected by AnalyticsScreen.kt.
"""

from pydantic import BaseModel
from typing import Optional


# ── 1. Total Spent Summary ─────────────────────────────────────────────────────

class TotalSpentResponse(BaseModel):
    total_spent    : float
    monthly_budget : float
    used_pct       : float   # 0–100
    remaining      : float
    savings_rate   : float   # (income - spent) / income × 100; 0 if no income set


# ── 2. Category Breakdown ──────────────────────────────────────────────────────

class CategoryItem(BaseModel):
    category   : str
    amount     : float
    percentage : float   # 0–100 share of total spend


class CategoryBreakdownResponse(BaseModel):
    total      : float
    categories : list[CategoryItem]


# ── 3. Monthly Trend ───────────────────────────────────────────────────────────

class MonthTrendItem(BaseModel):
    month  : str    # "Jan", "Feb", …
    year   : int
    amount : float


class MonthlyTrendResponse(BaseModel):
    trend_pct : float              # month-over-month % change (last two months)
    months    : list[MonthTrendItem]


# ── 4. Spending Heatmap ────────────────────────────────────────────────────────

class HeatmapResponse(BaseModel):
    year       : int
    month      : int                  # 1-based
    avg_spend  : float
    daily      : dict[str, float]     # { "1": 20.0, "5": 210.0, … } day → amount


# ── 5. Combined analytics (single endpoint option) ────────────────────────────

class AnalyticsSummaryResponse(BaseModel):
    summary            : TotalSpentResponse
    category_breakdown : CategoryBreakdownResponse
    monthly_trend      : MonthlyTrendResponse
    heatmap            : HeatmapResponse
