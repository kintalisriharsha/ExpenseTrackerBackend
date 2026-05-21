"""
goal_route.py
─────────────
All endpoints for the goals feature.

Endpoints
─────────
POST   /goals/add_goal            → create a new goal          (AddGoal.kt)
GET    /goals/all_goals                    → all goals, newest first     (GoalListScreen)
GET    /goals/active?limit=N      → top N active (incomplete) goals (HomeScreen banner)
PATCH  /goals/{goal_id}           → edit a goal                (GoalDetailScreen edit icon)
DELETE /goals/{goal_id}           → delete a goal              (GoalDetailScreen delete button)
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from auth.auth import get_current_user
from schemas.goal_schema import (
    GoalCreate,
    GoalUpdate,
    GoalResponse,
    GoalListResponse,
    VALID_CATEGORIES,
)
from crud.goal_crud import (
    add_goal,
    get_all_goals,
    get_goals_by_limit,
    edit_goal,
    delete_goal,
)

router = APIRouter(prefix="/goals", tags=["goals"])


# ── Add goal ───────────────────────────────────────────────────────────────────

@router.post(
    "/add_goal",
    response_model=GoalResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new savings goal",
    description=f"""
Called when the user taps **Create Goal** in `AddGoal` (SetGoal.kt).

- `goal_name`     — required, max 255 characters.
- `target_amount` — required, between **0.01** and **2,00,000**.
- `category`      — one of: `{sorted(VALID_CATEGORIES)}` (case-insensitive).
- `saved_amount`  — optional, defaults to **0**. Can be non-zero if the user
  is logging an already-started goal.

`is_completed` is managed by the server — it flips to `true` automatically
when `saved_amount >= target_amount`.

`progress_pct` (0–100) is pre-computed in the response so `CircularIndicator`
can use it directly.

Example request:
```json
{{
    "goal_name": "New Macbook M4 Air",
    "target_amount": 90000.00,
    "category": "Electronics"
}}
```

Example response:
```json
{{
    "id": 3,
    "user_id": 7,
    "goal_name": "New Macbook M4 Air",
    "target_amount": 90000.0,
    "saved_amount": 0.0,
    "progress_pct": 0.0,
    "category": "Electronics",
    "is_completed": false,
    "created_at": "2026-05-21T08:00:00Z",
    "updated_at": "2026-05-21T08:00:00Z"
}}
```
""",
)
async def add_goal_route(
    payload      : GoalCreate,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    return await add_goal(db, current_user["id"], payload)


# ── Get all goals ──────────────────────────────────────────────────────────────

@router.get(
    "/all_goals",
    response_model=GoalListResponse,
    summary="Get all goals — newest first, default limit 50",
    description="""
Returns every goal for the authenticated user, sorted **newest first**.

Use `limit` and `offset` for pagination. `total` in the response is the
grand total count (useful for "Showing 5 of 12" labels).

Example response:
```json
{
    "total": 5,
    "goals": [
        {
            "id": 3,
            "goal_name": "New Macbook M4 Air",
            "target_amount": 90000.0,
            "saved_amount": 12000.0,
            "progress_pct": 13.33,
            "category": "Electronics",
            "is_completed": false,
            ...
        }
    ]
}
```
""",
)
async def get_all_goals_route(
    limit        : int          = Query(50, ge=1, le=200, description="Max rows to return (default 50)"),
    offset       : int          = Query(0,  ge=0,         description="Rows to skip for pagination"),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    total, goals = await get_all_goals(db, current_user["id"], limit, offset)
    return GoalListResponse(total=total, goals=goals)


# ── Get by limit (active goals for HomeScreen banner) ─────────────────────────

@router.get(
    "/active",
    response_model=GoalListResponse,
    summary="Get top N active (incomplete) goals — used by HomeScreen SavingsBanner",
    description="""
Returns the most recent **incomplete** goals up to `limit`.

Used by `HomeScreen.kt`'s `SavingsBanner` to display the top savings goal.
Only returns goals where `is_completed = false`, ordered by `created_at DESC`.

`total` reflects the count of all active (incomplete) goals.

Example — get top 1 goal for the savings banner:
```
GET /goals/active?limit=1
```

Example response:
```json
{
    "total": 3,
    "goals": [
        {
            "id": 3,
            "goal_name": "New Macbook M4 Air",
            "target_amount": 90000.0,
            "saved_amount": 0.0,
            "progress_pct": 0.0,
            "category": "Electronics",
            "is_completed": false,
            ...
        }
    ]
}
```
""",
)
async def get_goals_by_limit_route(
    limit        : int          = Query(5, ge=1, le=50, description="Number of active goals to return (default 5)"),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    total, goals = await get_goals_by_limit(db, current_user["id"], limit)
    return GoalListResponse(total=total, goals=goals)


# ── Edit goal ──────────────────────────────────────────────────────────────────

@router.patch(
    "/{goal_id}",
    response_model=GoalResponse,
    summary="Edit a goal — PATCH semantics, only sent fields are updated",
    description="""
All fields are optional — only the fields included in the request body
are updated. Omit anything you don't want to change.

**Auto-completion:** After any update, if `saved_amount >= target_amount`
the server automatically sets `is_completed = true`. To re-open a completed
goal, explicitly pass `"is_completed": false`.

Raises **404** if the goal does not exist or belongs to a different user.
Raises **422** if the request body is completely empty.

Example — deposit ₹5,000 towards the goal:
```json
{ "saved_amount": 5000.00 }
```

Example — rename and change target:
```json
{
    "goal_name": "MacBook Pro M4",
    "target_amount": 150000.00
}
```

Example — manually mark complete:
```json
{ "is_completed": true }
```

Example — re-open a completed goal:
```json
{ "is_completed": false }
```
""",
)
async def edit_goal_route(
    goal_id      : int,
    payload      : GoalUpdate,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    return await edit_goal(db, current_user["id"], goal_id, payload)


# ── Delete goal ────────────────────────────────────────────────────────────────

@router.delete(
    "/{goal_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a goal",
    description="""
Hard-deletes the goal row.

Raises **404** if the goal does not exist or belongs to a different user.

Example response:
```json
{ "message": "Goal deleted successfully", "deleted_id": 3 }
```
""",
)
async def delete_goal_route(
    goal_id      : int,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    await delete_goal(db, current_user["id"], goal_id)
    return {"message": "Goal deleted successfully", "deleted_id": goal_id}