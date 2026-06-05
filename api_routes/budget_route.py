"""
budget_planner_route.py
───────────────────────
All endpoints for the BudgetScreen.kt weekly planner feature.

Endpoints
─────────
GET    /budget-planner/active
       → load BudgetScreen (auto-creates row if first time)

PATCH  /budget-planner/active/budget
       → user confirms WeeklyBudgetDialog

POST   /budget-planner/active/tasks
       → user confirms AddTaskBottomSheet

PATCH  /budget-planner/active/tasks/{task_id}
       → user confirms EditTaskBottomSheet OR toggles checkbox

DELETE /budget-planner/active/tasks/{task_id}
       → user taps delete icon in TaskRow

POST   /budget-planner/rollover
       → Android WorkManager fires every Monday morning

GET    /budget-planner/history
       → BudgetHistory tab in HistoryScreen
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from auth.auth import get_current_user
from schemas.budget_schema import (
    # SetWeeklyBudgetRequest,
    AddTaskRequest,
    UpdateTaskRequest,
    RolloverRequest,
    ActiveWeekResponse,
    TaskResponse,
    DeleteTaskResponse,
    # WeeklyBudgetResponse,
    HistoryListResponse,
    RolloverResponse,
)
from crud.budget_crud import (
    get_active_week,
    # set_weekly_budget,
    add_task,
    update_task,
    delete_task,
    rollover_week,
    get_history,
)

from cache import cache_delete, home_key

router = APIRouter(prefix="/budget-planner", tags=["budget-planner"])


# ── GET active week ────────────────────────────────────────────────────────────

@router.get(
    "/active",
    response_model=ActiveWeekResponse,
    status_code=status.HTTP_200_OK,
    summary="Load current week — called every time BudgetScreen opens",
    description="""
Returns the full week plan for the authenticated user.

Auto-creates the `budget_active` row if this user has never opened the
Budget Planner before — so Android never needs a separate init call.

`days` always contains exactly 7 entries (Mon–Sun) even if some are empty.

`weekly_exceeded` is pre-computed so Android can lock the + button
without any client-side arithmetic.

Example response:
```json
{
    "user_id": 7,
    "week_start": "2026-05-19",
    "week_end": "2026-05-25",
    "weekly_budget": 5000.0,
    "total_spent": 950.0,
    "weekly_exceeded": false,
    "days": [
        {
            "day_date": "2026-05-19",
            "tasks": [
                {"id": "uuid1", "name": "Groceries", "budget": 800.0, "is_done": true},
                {"id": "uuid2", "name": "Uber to office", "budget": 150.0, "is_done": false}
            ]
        },
        { "day_date": "2026-05-20", "tasks": [] },
        { "day_date": "2026-05-21", "tasks": [] },
        { "day_date": "2026-05-22", "tasks": [] },
        { "day_date": "2026-05-23", "tasks": [] },
        { "day_date": "2026-05-24", "tasks": [] },
        { "day_date": "2026-05-25", "tasks": [] }
    ],
    "updated_at": "2026-05-19T10:30:00Z"
}
```
""",
)
async def get_active_week_route(
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    return await get_active_week(db, current_user["id"])


# ── PATCH weekly budget ────────────────────────────────────────────────────────

# @router.patch(
#     "/active/budget",
#     response_model=WeeklyBudgetResponse,
#     status_code=status.HTTP_200_OK,
#     summary="Set or update the weekly budget cap — called from WeeklyBudgetDialog",
#     description="""
# Called when the user confirms the **WeeklyBudgetDialog** (taps "Confirm").

# Sets the `weekly_budget` cap for the current active week.
# Can be called again any time to edit the cap — maps to the "Edit" button
# on the WeeklyBudgetCard.

# Returns updated totals so Android can refresh the progress bar immediately.

# Example request:
# ```json
# { "weekly_budget": 5000.0 }
# ```

# Example response:
# ```json
# {
#     "weekly_budget": 5000.0,
#     "total_spent": 950.0,
#     "weekly_exceeded": false
# }
# ```
# """,
# )
# async def set_weekly_budget_route(
#     payload      : SetWeeklyBudgetRequest,
#     db           : AsyncSession = Depends(get_db),
#     current_user : dict         = Depends(get_current_user),
# ):
#     return await set_weekly_budget(db, current_user["id"], payload.weekly_budget)


# ── POST add task ──────────────────────────────────────────────────────────────

@router.post(
    "/active/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a task to a day — called from AddTaskBottomSheet",
    description="""
Called when the user confirms **AddTaskBottomSheet** (taps "Add Task").

`id` must be a client-generated UUID. The Android app generates this
before posting so it can reference the task immediately in the UI
without waiting for a server round-trip.

`day_date` must be within the current active week (Mon–Sun).
Returns 400 if the date is outside the current week.

Returns 422 if the weekly budget is set and adding this task would
exceed it — matches the `isLocked` check in `DayBlock`.

Returns updated totals so Android can refresh the WeeklyBudgetCard
progress bar without a second GET call.

Example request:
```json
{
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "day_date": "2026-05-19",
    "name": "Groceries",
    "budget": 800.0
}
```

Example response:
```json
{
    "task": {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "name": "Groceries",
        "budget": 800.0,
        "is_done": false
    },
    "day_date": "2026-05-19",
    "total_spent": 1750.0,
    "weekly_budget": 5000.0,
    "weekly_exceeded": false
}
```
""",
)
async def add_task_route(
    payload      : AddTaskRequest,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    
    result = await add_task(db, current_user["id"], payload)
    await cache_delete(home_key(current_user["id"]))
    return result


# ── PATCH update task ──────────────────────────────────────────────────────────

@router.patch(
    "/active/tasks/{task_id}",
    response_model=TaskResponse,
    status_code=status.HTTP_200_OK,
    summary="Edit or toggle a task — called from EditTaskBottomSheet or checkbox",
    description="""
PATCH semantics — only fields included in the request body are updated.

Two use cases:

**1. User toggles the checkbox (TaskRow)**
```json
{ "is_done": true }
```

**2. User edits from EditTaskBottomSheet**
```json
{
    "name": "Weekly groceries",
    "budget": 950.0,
    "is_done": false
}
```

`task_id` is the UUID that was sent when the task was created.
Returns 404 if the task is not found in the active week.
Returns 422 if the request body contains no fields at all.

Returns updated totals so Android can refresh the progress bar
without a second GET call.

Example response:
```json
{
    "task": {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "name": "Weekly groceries",
        "budget": 950.0,
        "is_done": false
    },
    "day_date": "2026-05-19",
    "total_spent": 1900.0,
    "weekly_budget": 5000.0,
    "weekly_exceeded": false
}
```
""",
)
async def update_task_route(
    task_id      : str,
    payload      : UpdateTaskRequest,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    result = await update_task(db, current_user["id"], task_id, payload)
    await cache_delete(home_key(current_user["id"]))
    return result


# ── DELETE task ────────────────────────────────────────────────────────────────

@router.delete(
    "/active/tasks/{task_id}",
    response_model=DeleteTaskResponse,
    status_code=status.HTTP_200_OK,
    summary="Delete a task — called from the delete icon in TaskRow",
    description="""
Hard-deletes a task from the active week's JSON blob.
Called when the user taps the **delete icon (trash)** in `TaskRow`.

Returns 404 if the task is not found in the active week.

Returns updated totals so Android can refresh the WeeklyBudgetCard
and unlock the + button if the budget is no longer exceeded.

Example response:
```json
{
    "deleted_id": "550e8400-e29b-41d4-a716-446655440000",
    "total_spent": 950.0,
    "weekly_budget": 5000.0,
    "weekly_exceeded": false
}
```
""",
)
async def delete_task_route(
    task_id      : str,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    result = await delete_task(db, current_user["id"], task_id)
    await cache_delete(home_key(current_user["id"]))
    return result


# ── POST rollover ──────────────────────────────────────────────────────────────

@router.post(
    "/rollover",
    response_model=RolloverResponse,
    status_code=status.HTTP_200_OK,
    summary="Monday rollover — called by Android WorkManager at start of new week",
    description="""
**Called by Android WorkManager every Monday morning.**

Steps performed by the server:
1. Archive the current active week → writes a row to `budget_history`
2. Reset `budget_active` for the new week (empty tasks, new week_start)
3. Optionally carry forward the weekly budget cap

**Idempotent:** If rollover has already been done for this Monday
(e.g. WorkManager fires twice), returns `action: "already_rolled"`
with no data changes.

`carry_forward_budget`:
- `true`  (default) → new week starts with same weekly_budget cap
- `false` → new week starts with weekly_budget = 0 (user sets it fresh)

Example request:
```json
{ "carry_forward_budget": true }
```

Example response — first rollover of the week:
```json
{
    "action": "rolled_over",
    "archived_week_start": "2026-05-12",
    "new_week_start": "2026-05-19",
    "carried_forward_budget": 5000.0
}
```

Example response — WorkManager fired twice on same Monday:
```json
{
    "action": "already_rolled",
    "archived_week_start": null,
    "new_week_start": "2026-05-19",
    "carried_forward_budget": 5000.0
}
```
""",
)
async def rollover_route(
    payload      : RolloverRequest,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    return await rollover_week(
        db,
        current_user["id"],
        payload.carry_forward_budget,
    )


# ── GET history ────────────────────────────────────────────────────────────────

@router.get(
    "/history",
    response_model=HistoryListResponse,
    status_code=status.HTTP_200_OK,
    summary="Past weeks — used by BudgetHistory tab in HistoryScreen",
    description="""
Returns archived weeks for the **Budget History** tab, newest first.

`within_budget` is pre-computed:
- `true`  → total_spent ≤ weekly_budget (or no budget was set)
- `false` → total_spent > weekly_budget

`tasks_data` is included so Android can show a full task breakdown
when the user taps a history row — no second API call needed.

Use `limit` and `offset` for pagination.
`total` is the grand total archived week count (for "Showing 5 of 12" labels).

Example response:
```json
{
    "total": 12,
    "weeks": [
        {
            "id": 12,
            "week_start": "2026-05-12",
            "week_end": "2026-05-18",
            "weekly_budget": 5000.0,
            "total_spent": 4120.0,
            "within_budget": true,
            "tasks_data": {
                "2026-05-12": [
                    {"id": "uuid1", "name": "Groceries", "budget": 800.0, "is_done": true}
                ],
                "2026-05-13": []
            },
            "created_at": "2026-05-19T06:00:00Z"
        }
    ]
}
```
""",
)
async def get_history_route(
    limit        : int          = Query(10, ge=1, le=52, description="Weeks to return (default 10, max 52)"),
    offset       : int          = Query(0,  ge=0,        description="Rows to skip for pagination"),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    return await get_history(db, current_user["id"], limit, offset)