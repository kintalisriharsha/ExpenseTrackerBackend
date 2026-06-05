"""
setting_route.py
────────────────
All endpoints for the settings feature.

CACHING STRATEGY
────────────────
GET /setting/current → cached as settings:{user_id}  (TTL_SETTINGS = 15 min)
  This is called on every app open and Settings screen load.
  Busted by: PATCH /setting, POST /setting/init, POST /setting/carry-forward

GET /setting/year/{year} → NOT cached
  Called rarely (only on the year overview screen), low query cost,
  and would need year-qualified keys with no clear invalidation trigger.
"""

from fastapi import APIRouter, Depends, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date

from db import get_db
from auth.auth import get_current_user
from schemas.setting_schema import (
    SettingsInit, SettingsUpdate, SettingsResponse,
    YearSettingsResponse, CarryForwardRequest, CarryForwardResponse,
    MonthEntry, MONTHS,
)
from crud.setting_crud import (
    init_settings, update_settings, get_settings_or_404,
    get_current_month, get_year, carry_forward_month,
)
from cache import cache_get, cache_set, cache_delete, settings_key, home_key, TTL_SETTINGS

router = APIRouter(prefix="/setting", tags=["setting"])


def _resolve_current() -> tuple[str, str]:
    today = date.today()
    return str(today.year), MONTHS[today.month - 1]


async def _bust_settings_caches(user_id: int) -> None:
    """Settings change also affects home (budget limits come from Settings)."""
    await cache_delete(settings_key(user_id), home_key(user_id))


# ── Init ───────────────────────────────────────────────────────────────────────

@router.post(
    "/init",
    response_model=SettingsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initialise settings — call once at onboarding",
)
async def init_settings_route(
    payload      : SettingsInit,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    settings, entry = await init_settings(db, current_user["id"], payload)

    # New settings row — bust cache so /current reflects it immediately
    await _bust_settings_caches(current_user["id"])

    year_str, month_str = _resolve_current()
    return SettingsResponse(
        year=int(year_str), month=month_str,
        monthly_budget=entry["monthly_budget"],
        weekly_budget=entry["weekly_budget"],
        daily_limit=entry["daily_limit"],
        notification_enabled=settings.notification_enabled,
        is_dark_mode=settings.is_dark_mode,
        user_monthly_budget_synced=entry["monthly_budget"],
        user_daily_limit_synced=entry["daily_limit"],
    )


# ── GET current month (cached) ─────────────────────────────────────────────────

@router.get(
    "/current",
    response_model=SettingsResponse,
    summary="Get current month's settings",
)
async def get_current_month_route(
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    user_id = current_user["id"]
    key     = settings_key(user_id)

    cached = await cache_get(key)
    if cached:
        return cached

    year_str, month_str, entry = await get_current_month(db, user_id)
    settings = await get_settings_or_404(db, user_id)

    response = SettingsResponse(
        year=int(year_str), month=month_str,
        monthly_budget=entry["monthly_budget"],
        weekly_budget=entry["weekly_budget"],
        daily_limit=entry["daily_limit"],
        notification_enabled=settings.notification_enabled,
        is_dark_mode=settings.is_dark_mode,
        user_monthly_budget_synced=float(current_user.get("monthly_budget", 0.0)),
        user_daily_limit_synced=float(current_user.get("daily_budget", 0.0)),
    )

    await cache_set(key, jsonable_encoder(response), TTL_SETTINGS)
    return response


# ── GET year (not cached — called rarely) ─────────────────────────────────────

@router.get(
    "/year/{year}",
    response_model=YearSettingsResponse,
    summary="Get all 12 months for a year",
)
async def get_year_route(
    year         : int,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    months_raw = await get_year(db, current_user["id"], year)
    return YearSettingsResponse(
        year=year,
        months={
            m: MonthEntry(
                monthly_budget=months_raw[m]["monthly_budget"],
                weekly_budget=months_raw[m]["weekly_budget"],
                daily_limit=months_raw[m]["daily_limit"],
            )
            for m in MONTHS
        },
    )


# ── PATCH (bust cache) ─────────────────────────────────────────────────────────

@router.patch(
    "",
    response_model=SettingsResponse,
    summary="Save settings from Settings screen",
)
async def update_settings_route(
    payload      : SettingsUpdate,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    settings, year_str, month_str, entry = await update_settings(
        db, current_user["id"], payload
    )

    await _bust_settings_caches(current_user["id"])

    return SettingsResponse(
        year=int(year_str), month=month_str,
        monthly_budget=entry["monthly_budget"],
        weekly_budget=entry["weekly_budget"],
        daily_limit=entry["daily_limit"],
        notification_enabled=settings.notification_enabled,
        is_dark_mode=settings.is_dark_mode,
        user_monthly_budget_synced=entry["monthly_budget"],
        user_daily_limit_synced=entry["daily_limit"],
    )


# ── Carry-forward (bust cache) ─────────────────────────────────────────────────

@router.post(
    "/carry-forward",
    response_model=CarryForwardResponse,
    summary="Carry previous month's settings into the new month",
)
async def carry_forward_route(
    payload      : CarryForwardRequest,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    result = await carry_forward_month(
        db=db, user_id=current_user["id"], overwrite=payload.overwrite,
    )

    # Always bust — even "already_set" is worth refreshing
    await _bust_settings_caches(current_user["id"])

    return CarryForwardResponse(
        action=result["action"],
        target_period=result["target_period"],
        source_period=result["source_period"],
        values_applied=MonthEntry(**result["values_applied"]),
        already_had=MonthEntry(**result["already_had"]) if result["already_had"] else None,
        user_monthly_budget_synced=result.get("user_monthly_budget_synced"),
        user_daily_limit_synced=result.get("user_daily_limit_synced"),
    )