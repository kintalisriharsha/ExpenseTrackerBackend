"""
setting_route.py
────────────────
All endpoints for the settings feature.

Endpoints
─────────
POST   /setting/init              → first time setup (onboarding)
GET    /setting/current           → load Settings screen with current month's values
GET    /setting/year/{year}       → all 12 months for a given year
PATCH  /setting                   → save from Settings screen
POST   /setting/carry-forward     → Android WorkManager: new month started
DELETE /setting                   → delete settings record
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from auth.auth import get_current_user
from schemas.setting_schema import (
    SettingsInit,
    SettingsUpdate,
    SettingsResponse,
    YearSettingsResponse,
    CarryForwardRequest,
    CarryForwardResponse,
    MonthEntry,
    MONTHS,
)
from crud.setting_crud import (
    init_settings,
    update_settings,
    get_settings_or_404,
    get_current_month,
    get_year,
    carry_forward_month
)

from datetime import date
from schemas.setting_schema import MONTHS

router = APIRouter(prefix="/setting", tags=["setting"])


# ── Init ───────────────────────────────────────────────────────────────────────

@router.post(
    "/init",
    response_model=SettingsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initialise settings — call once at onboarding",
    description="""
Creates the settings record for the authenticated user.

- `apply_to_all_months: true` → seeds all 12 months of the current year
  with the same values. Good for first-time setup.
- `apply_to_all_months: false` (default) → only seeds the current month.

Raises **409** if settings already exists.

Example:
```json
{
    "monthly_budget": 4500.0,
    "daily_limit": 150.0,
    "notification_enabled": true,
    "is_dark_mode": false,
    "apply_to_all_months": true
}
```
""",
)
async def init_settings_route(
    payload      : SettingsInit,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    settings, entry = await init_settings(db, current_user["id"], payload)
    year_str, month_str = _resolve_current()
    return SettingsResponse(
        year                       = int(year_str),
        month                      = month_str,
        monthly_budget             = entry["monthly_budget"],
        daily_limit                = entry["daily_limit"],
        notification_enabled       = settings.notification_enabled,
        is_dark_mode               = settings.is_dark_mode,
        user_monthly_budget_synced = entry["monthly_budget"],
        user_daily_limit_synced    = entry["daily_limit"],
    )


# ── Read ───────────────────────────────────────────────────────────────────────

@router.get(
    "/current",
    response_model=SettingsResponse,
    summary="Get current month's settings — used to load the Settings screen",
    description="""
Returns the `monthly_budget`, `daily_limit`, `notification_enabled`, and
`is_dark_mode` for the current month.

If the current month has no entry yet (e.g. it's a brand-new month and
the WorkManager job hasn't fired yet), the previous month's values are
automatically carried forward and returned — so the Settings screen always
shows a sensible pre-filled value.

Example response:
```json
{
    "year": 2026,
    "month": "may",
    "monthly_budget": 4500.0,
    "daily_limit": 150.0,
    "notification_enabled": true,
    "is_dark_mode": false,
    "user_monthly_budget_synced": 4500.0,
    "user_daily_limit_synced": 150.0
}
```
""",
)
async def get_current_month_route(
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    year_str, month_str, entry = await get_current_month(db, current_user["id"])
    settings = await get_settings_or_404(db, current_user["id"])
    return SettingsResponse(
        year                       = int(year_str),
        month                      = month_str,
        monthly_budget             = entry["monthly_budget"],
        daily_limit                = entry["daily_limit"],
        notification_enabled       = settings.notification_enabled,
        is_dark_mode               = settings.is_dark_mode,
        user_monthly_budget_synced = float(current_user.get("monthly_budget", 0.0)),
        user_daily_limit_synced    = float(current_user.get("daily_budget",   0.0)),
    )


@router.get(
    "/year/{year}",
    response_model=YearSettingsResponse,
    summary="Get all 12 months for a year",
    description="""
Returns the `monthly_budget` and `daily_limit` for every month in the
given year. Months with no data return `{ monthly_budget: 0.0, daily_limit: 0.0 }`.

Useful for displaying a full-year settings overview screen.

Example response:
```json
{
    "year": 2026,
    "months": {
        "jan": { "monthly_budget": 4500.0, "daily_limit": 150.0 },
        "feb": { "monthly_budget": 4200.0, "daily_limit": 130.0 },
        ...
        "dec": { "monthly_budget": 5000.0, "daily_limit": 160.0 }
    }
}
```
""",
)
async def get_year_route(
    year         : int,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    months_raw = await get_year(db, current_user["id"], year)
    return YearSettingsResponse(
        year   = year,
        months = {
            m: MonthEntry(
                monthly_budget = months_raw[m]["monthly_budget"],
                daily_limit    = months_raw[m]["daily_limit"],
            )
            for m in MONTHS
        },
    )


# ── Update ─────────────────────────────────────────────────────────────────────

@router.patch(
    "",
    response_model=SettingsResponse,
    summary="Save settings from Settings screen",
    description="""
Called every time the user taps **Save All Settings**.

- `notification_enabled` and `is_dark_mode` are always saved to the
  `Settings` row regardless of which month is being updated.
- If `year` and `month` are omitted → updates the **current** month.
- If provided → updates that specific month (e.g. editing a past month).
- Always syncs `User.monthly_budget` and `User.daily_budget` when the
  target month is the current month.

Example — save current month:
```json
{
    "monthly_budget": 4800.0,
    "daily_limit": 160.0,
    "notification_enabled": true,
    "is_dark_mode": true
}
```

Example — save a specific month:
```json
{
    "year": 2026,
    "month": "feb",
    "monthly_budget": 4200.0,
    "daily_limit": 130.0,
    "notification_enabled": true,
    "is_dark_mode": false
}
```
""",
)
async def update_settings_route(
    payload      : SettingsUpdate,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    settings, year_str, month_str, entry = await update_settings(
        db, current_user["id"], payload
    )
    return SettingsResponse(
        year                       = int(year_str),
        month                      = month_str,
        monthly_budget             = entry["monthly_budget"],
        daily_limit                = entry["daily_limit"],
        notification_enabled       = settings.notification_enabled,
        is_dark_mode               = settings.is_dark_mode,
        user_monthly_budget_synced = entry["monthly_budget"],
        user_daily_limit_synced    = entry["daily_limit"],
    )


# ── Carry-forward ──────────────────────────────────────────────────────────────

@router.post(
    "/carry-forward",
    response_model=CarryForwardResponse,
    summary="Carry previous month's settings into the new month",
    description="""
**Called by Android WorkManager at the start of every new month.**

Copies the previous month's `monthly_budget` and `daily_limit` into the
current month so the user always sees sensible pre-filled values in Settings
without having to reconfigure everything manually.

Note: `notification_enabled` and `is_dark_mode` are **never** carried
forward — they are sticky preferences that only change when the user
explicitly toggles them in Settings.

**Safe by default (`overwrite: false`):**
If the user already edited the current month manually, nothing is changed
and `action: "already_set"` is returned — their data is never lost.

**Force replace (`overwrite: true`):**
Replaces the current month's values with last month's — useful as a
"Reset to last month" action if you add that button to the Settings screen.

**Year boundary handled automatically:**
If today is January, the source is December of the previous year.

Possible `action` values in the response:
- `"carried_forward"` — month was empty, values copied in ✅
- `"already_set"`     — month already had values, nothing changed ℹ️
- `"overwritten"`     — month had values but overwrite=true forced a replace ⚠️

Example response (new month, nothing set):
```json
{
    "action": "carried_forward",
    "target_period": "jun 2026",
    "source_period": "may 2026",
    "values_applied": { "monthly_budget": 4500.0, "daily_limit": 150.0 },
    "already_had": null,
    "user_monthly_budget_synced": 4500.0,
    "user_daily_limit_synced": 150.0
}
```

Example response (month already configured):
```json
{
    "action": "already_set",
    "target_period": "jun 2026",
    "source_period": "may 2026",
    "values_applied": { "monthly_budget": 5000.0, "daily_limit": 180.0 },
    "already_had":    { "monthly_budget": 5000.0, "daily_limit": 180.0 },
    "user_monthly_budget_synced": null,
    "user_daily_limit_synced": null
}
```
""",
)
async def carry_forward_route(
    payload      : CarryForwardRequest,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    result = await carry_forward_month(
        db        = db,
        user_id   = current_user["id"],
        overwrite = payload.overwrite,
    )
    return CarryForwardResponse(
        action          = result["action"],
        target_period   = result["target_period"],
        source_period   = result["source_period"],
        values_applied  = MonthEntry(**result["values_applied"]),
        already_had     = MonthEntry(**result["already_had"]) if result["already_had"] else None,
        user_monthly_budget_synced = result.get("user_monthly_budget_synced"),
        user_daily_limit_synced    = result.get("user_daily_limit_synced"),
    )


# ── Private helper ─────────────────────────────────────────────────────────────

def _resolve_current() -> tuple[str, str]:
    today = date.today()
    return str(today.year), MONTHS[today.month - 1]
