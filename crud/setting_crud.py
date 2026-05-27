"""
setting_crud.py
───────────────
All database operations for the settings feature.

JSON structure operated on:
{
    "2026": {
        "jan": { "monthly_budget": 4500.0, "weekly_budget": 1000.0, "daily_limit": 150.0 },
        "feb": { "monthly_budget": 4200.0, "weekly_budget": 900.0,  "daily_limit": 130.0 },
        ...
    }
}

Sync rule:
    After every write, the current month's values are written back to
    User.monthly_budget and User.daily_budget so the rest of the app
    always has fresh values from the JWT / profile endpoint.

Carry-forward rule:
    When Android WorkManager calls carry_forward_month(), if the new month
    has no entry yet, the previous month's values are copied in untouched.
    The user can then edit from Settings at any time.

Preference rule:
    notification_enabled and is_dark_mode are sticky columns on Settings.
    They are written on every init/update but never carry-forwarded.
"""

from __future__ import annotations

import copy
import logging
from datetime import date
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.setting_model import Settings
from models.user_model import User
from schemas.setting_schema import (
    SettingsInit,
    SettingsUpdate,
    MONTHS,
    _empty_month_entry,
)

logger = logging.getLogger(__name__)


# ── Private helpers ────────────────────────────────────────────────────────────

def _today_year_month() -> tuple[str, str]:
    """Return (year_str, month_abbr) for today. e.g. ('2026', 'may')"""
    today = date.today()
    return str(today.year), MONTHS[today.month - 1]


def _prev_month_key(year: int, month_idx: int) -> tuple[int, int]:
    """
    Given a 1-based month index, return (year, month_idx) for the previous month.
    Handles January → December of previous year.
    """
    if month_idx == 1:
        return year - 1, 12
    return year, month_idx - 1


def _get_month_entry(budget_data: dict, year_str: str, month_str: str) -> dict:
    """Safely read one month's entry, returning zeros if not found."""
    return budget_data.get(year_str, {}).get(month_str, _empty_month_entry())


async def _sync_to_user(
    db      : AsyncSession,
    user_id : int,
    entry   : dict,
) -> None:
    """
    Write the given month entry's values directly to the User row.
    Uses UPDATE statement — no extra SELECT needed.
    """
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            monthly_budget = float(entry.get("monthly_budget", 0.0)),
            weekly_budget = float(entry.get("weekly_budget", 0.0)),
            daily_budget   = float(entry.get("daily_limit",    0.0)),
        )
    )
    await db.flush()
    logger.info(
        f"Synced user_id={user_id}: "
        f"monthly_budget={entry.get('monthly_budget')}, "
        f"weekly_budget={entry.get('weekly_budget')}, "
        f"daily_budget={entry.get('daily_limit')}"
    )


# ── Validation helper ──────────────────────────────────────────────────────────

def _validate_budget_hierarchy(monthly_budget: float, weekly_budget: float, daily_limit: float) -> None:
    """
    Enforce: daily_limit <= weekly_budget <= monthly_budget.
    Raises 422 if any constraint is violated.
    """
    if weekly_budget > monthly_budget:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"weekly_budget ({weekly_budget}) cannot exceed monthly_budget ({monthly_budget}).",
        )


# ══════════════════════════════════════════════════════════════════════════════
# FETCH HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def get_settings(db: AsyncSession, user_id: int) -> Optional[Settings]:
    result = await db.execute(
        select(Settings).where(Settings.user_id == user_id)
    )
    return result.scalars().first()


async def get_settings_or_404(db: AsyncSession, user_id: int) -> Settings:
    settings = await get_settings(db, user_id)
    if not settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Settings not found. Call POST /setting/init first.",
        )
    return settings


# ══════════════════════════════════════════════════════════════════════════════
# INIT  (called once at onboarding / first Settings save)
# ══════════════════════════════════════════════════════════════════════════════

async def init_settings(
    db      : AsyncSession,
    user_id : int,
    payload : SettingsInit,
) -> tuple[Settings, dict]:
    """
    Create a Settings row for the user.
    Raises 409 if one already exists.
    Raises 422 if weekly_budget > monthly_budget.

    Persists notification_enabled and is_dark_mode from the payload.

    Returns (settings, current_month_entry).
    """
    existing = await get_settings(db, user_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Settings already initialised. Use PATCH /setting to update.",
        )

    _validate_budget_hierarchy(
        payload.monthly_budget,
        payload.weekly_budget,
        payload.daily_limit,
    )

    year_str, month_str = _today_year_month()
    entry = {
        "monthly_budget": payload.monthly_budget,
        "weekly_budget" : payload.weekly_budget,
        "daily_limit"   : payload.daily_limit,
    }

    # Build budget_data
    if payload.apply_to_all_months:
        budget_data = {year_str: {m: entry.copy() for m in MONTHS}}
    else:
        budget_data = {year_str: {month_str: entry.copy()}}

    settings = Settings(
        user_id              = user_id,
        budget_data          = budget_data,
        notification_enabled = payload.notification_enabled,
        is_dark_mode         = payload.is_dark_mode,
    )
    db.add(settings)
    await db.flush()

    await _sync_to_user(db, user_id, entry)
    logger.info(f"Settings initialised for user_id={user_id}")
    return settings, entry


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE  (called every time user taps Save in Settings)
# ══════════════════════════════════════════════════════════════════════════════

async def update_settings(
    db      : AsyncSession,
    user_id : int,
    payload : SettingsUpdate,
) -> tuple[Settings, str, str, dict]:
    """
    Save the monthly_budget, weekly_budget, and daily_limit for a specific month,
    and always persist notification_enabled and is_dark_mode to the row.
    Defaults to the current year/month if not provided in payload.
    Raises 422 if weekly_budget > monthly_budget.

    Returns (settings, year_str, month_str, entry).
    """
    _validate_budget_hierarchy(
        payload.monthly_budget,
        payload.weekly_budget,
        payload.daily_limit,
    )

    settings = await get_settings_or_404(db, user_id)

    # Resolve target year/month
    today = date.today()
    year_str  = str(payload.year)     if payload.year  else str(today.year)
    month_str = payload.month.lower() if payload.month else MONTHS[today.month - 1]

    entry = {
        "monthly_budget": payload.monthly_budget,
        "weekly_budget" : payload.weekly_budget,
        "daily_limit"   : payload.daily_limit,
    }

    # Deep copy → mutate → reassign (required for SQLAlchemy JSON detection)
    new_data = copy.deepcopy(settings.budget_data)
    new_data.setdefault(year_str, {})[month_str] = entry
    settings.budget_data = new_data

    # Preferences are sticky columns — always update them regardless of month
    settings.notification_enabled = payload.notification_enabled
    settings.is_dark_mode         = payload.is_dark_mode

    await db.flush()

    # Only sync to User if this is the current month
    cur_year, cur_month = _today_year_month()
    if year_str == cur_year and month_str == cur_month:
        await _sync_to_user(db, user_id, entry)

    logger.info(
        f"Settings updated for user_id={user_id} "
        f"{month_str}/{year_str}: {entry} | "
        f"notification_enabled={payload.notification_enabled} "
        f"is_dark_mode={payload.is_dark_mode}"
    )
    return settings, year_str, month_str, entry


# ══════════════════════════════════════════════════════════════════════════════
# READ
# ══════════════════════════════════════════════════════════════════════════════

async def get_current_month(
    db      : AsyncSession,
    user_id : int,
) -> tuple[str, str, dict]:
    """
    Returns the current month's settings entry.
    If it doesn't exist yet, carry-forward is triggered automatically
    so the Settings screen always gets a sensible value.

    Returns (year_str, month_str, entry).
    """
    settings = await get_settings_or_404(db, user_id)
    year_str, month_str = _today_year_month()

    entry = _get_month_entry(settings.budget_data, year_str, month_str)

    # Auto carry-forward if current month has no data
    if entry == _empty_month_entry():
        _, _, entry = await _carry_forward(db, user_id, settings, overwrite=False)

    return year_str, month_str, entry


async def get_year(
    db      : AsyncSession,
    user_id : int,
    year    : int,
) -> dict:
    """All months for a given year. Missing months return zeros."""
    settings = await get_settings_or_404(db, user_id)
    year_str  = str(year)
    year_data = settings.budget_data.get(year_str, {})

    return {
        m: year_data.get(m, _empty_month_entry())
        for m in MONTHS
    }


# ══════════════════════════════════════════════════════════════════════════════
# CARRY-FORWARD  (called by Android WorkManager at month boundary)
# ══════════════════════════════════════════════════════════════════════════════

async def _carry_forward(
    db        : AsyncSession,
    user_id   : int,
    settings  : Settings,
    overwrite : bool,
) -> tuple[str, dict, dict]:
    """
    Internal carry-forward logic shared by the public endpoint and auto-trigger.
    Preferences (notification_enabled, is_dark_mode) are never touched here.
    weekly_budget is carried forward alongside monthly_budget and daily_limit.

    Returns (action, source_entry, target_entry).
    """
    today      = date.today()
    cur_year   = today.year
    cur_month  = today.month   # 1-based

    cur_year_str  = str(cur_year)
    cur_month_str = MONTHS[cur_month - 1]

    prev_year_int, prev_month_int = _prev_month_key(cur_year, cur_month)
    prev_year_str  = str(prev_year_int)
    prev_month_str = MONTHS[prev_month_int - 1]

    current_entry = _get_month_entry(
        settings.budget_data, cur_year_str, cur_month_str
    )
    already_set = current_entry != _empty_month_entry()

    if already_set and not overwrite:
        return "already_set", current_entry, current_entry

    source_entry = _get_month_entry(
        settings.budget_data, prev_year_str, prev_month_str
    )

    new_data = copy.deepcopy(settings.budget_data)
    new_data.setdefault(cur_year_str, {})[cur_month_str] = source_entry.copy()
    settings.budget_data = new_data
    await db.flush()

    await _sync_to_user(db, user_id, source_entry)

    action = "overwritten" if already_set else "carried_forward"
    logger.info(
        f"carry_forward user_id={user_id}: "
        f"{prev_month_str}/{prev_year_str} → {cur_month_str}/{cur_year_str} "
        f"action={action} values={source_entry}"
    )
    return action, source_entry, current_entry


async def carry_forward_month(
    db        : AsyncSession,
    user_id   : int,
    overwrite : bool = False,
) -> dict:
    """
    Public entry point called by the Android WorkManager job
    at the start of every new month.

    Returns a result dict for the route to build its response from.
    """
    settings = await get_settings_or_404(db, user_id)

    today         = date.today()
    cur_year_str  = str(today.year)
    cur_month_str = MONTHS[today.month - 1]

    prev_year_int, prev_month_int = _prev_month_key(today.year, today.month)
    prev_year_str  = str(prev_year_int)
    prev_month_str = MONTHS[prev_month_int - 1]

    action, source_entry, had_before = await _carry_forward(
        db, user_id, settings, overwrite
    )

    settings   = await get_settings_or_404(db, user_id)   # refresh after flush
    values_now = _get_month_entry(settings.budget_data, cur_year_str, cur_month_str)

    return {
        "action"        : action,
        "target_period" : f"{cur_month_str} {cur_year_str}",
        "source_period" : f"{prev_month_str} {prev_year_str}",
        "values_applied": values_now,
        "already_had"   : had_before if action == "already_set" else None,
        "user_monthly_budget_synced": values_now.get("monthly_budget") if action != "already_set" else None,
        "user_daily_limit_synced"   : values_now.get("daily_limit")    if action != "already_set" else None,
    }
