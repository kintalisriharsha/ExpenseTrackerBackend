"""
goal_route.py
─────────────
All endpoints for the goals feature.

CACHING STRATEGY
────────────────
Two cached keys per user:
  goals:all:{user_id}    → /all_goals  (TTL_GOALS = 10 min)
  goals:active:{user_id} → /active     (TTL_GOALS = 10 min)

Both are busted together on any goal mutation (add / edit / delete)
since a change to goals affects both lists.

NOTE: /active is also called by the HomeScreen (SavingsBanner).
The home_key cache is busted separately — goal mutations don't bust
home because the home route fetches its own goal directly from DB.
If you want goals changes to instantly reflect on home, add
home_key(user_id) to _bust_goal_caches below.
"""

from fastapi import APIRouter, Depends, Query, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from auth.auth import get_current_user
from schemas.goal_schema import GoalCreate, GoalUpdate, GoalResponse, GoalListResponse, VALID_CATEGORIES
from crud.goal_crud import add_goal, get_all_goals, get_goals_by_limit, edit_goal, delete_goal
from cache import cache_get, cache_set, cache_delete, goals_all_key, goals_active_key, home_key, TTL_GOALS

router = APIRouter(prefix="/goals", tags=["goals"])


async def _bust_goal_caches(user_id: int) -> None:
    """
    Bust both goal list caches + home (SavingsBanner on HomeScreen
    shows the top active goal — a change should reflect immediately).
    """
    await cache_delete(
        goals_all_key(user_id),
        goals_active_key(user_id),
        home_key(user_id),
    )


# ── Add goal ───────────────────────────────────────────────────────────────────

@router.post(
    "/add_goal",
    response_model=GoalResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new savings goal",
)
async def add_goal_route(
    payload      : GoalCreate,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    result = await add_goal(db, current_user["id"], payload)
    await _bust_goal_caches(current_user["id"])
    return result


# ── Get all goals (cached) ─────────────────────────────────────────────────────

@router.get(
    "/all_goals",
    response_model=GoalListResponse,
    summary="Get all goals — newest first",
)
async def get_all_goals_route(
    limit        : int          = Query(50, ge=1, le=200),
    offset       : int          = Query(0,  ge=0),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    user_id = current_user["id"]

    # Only cache the default page (limit=50, offset=0) — the common case.
    # Paginated requests beyond that always go to DB.
    if limit == 50 and offset == 0:
        key    = goals_all_key(user_id)
        cached = await cache_get(key)
        if cached:
            return cached

        total, goals = await get_all_goals(db, user_id, limit, offset)
        response = GoalListResponse(total=total, goals=goals)
        await cache_set(key, jsonable_encoder(response), TTL_GOALS)
        return response

    total, goals = await get_all_goals(db, user_id, limit, offset)
    return GoalListResponse(total=total, goals=goals)


# ── Active goals for HomeScreen banner (cached) ────────────────────────────────

@router.get(
    "/active",
    response_model=GoalListResponse,
    summary="Get top N active goals — used by HomeScreen SavingsBanner",
)
async def get_goals_by_limit_route(
    limit        : int          = Query(5, ge=1, le=50),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    user_id = current_user["id"]

    # Cache the default limit=5 request (the HomeScreen banner call)
    if limit == 5:
        key    = goals_active_key(user_id)
        cached = await cache_get(key)
        if cached:
            return cached

        total, goals = await get_goals_by_limit(db, user_id, limit)
        response = GoalListResponse(total=total, goals=goals)
        await cache_set(key, jsonable_encoder(response), TTL_GOALS)
        return response

    total, goals = await get_goals_by_limit(db, user_id, limit)
    return GoalListResponse(total=total, goals=goals)


# ── Edit goal ──────────────────────────────────────────────────────────────────

@router.patch(
    "/{goal_id}",
    response_model=GoalResponse,
    summary="Edit a goal",
)
async def edit_goal_route(
    goal_id      : int,
    payload      : GoalUpdate,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    result = await edit_goal(db, current_user["id"], goal_id, payload)
    await _bust_goal_caches(current_user["id"])
    return result


# ── Delete goal ────────────────────────────────────────────────────────────────

@router.delete(
    "/{goal_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a goal",
)
async def delete_goal_route(
    goal_id      : int,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    await delete_goal(db, current_user["id"], goal_id)
    await _bust_goal_caches(current_user["id"])
    return {"message": "Goal deleted successfully", "deleted_id": goal_id}