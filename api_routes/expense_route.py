"""
expense_route.py
────────────────
All endpoints for the expense feature.

Endpoints
─────────
POST   /expenses/add_expense                  → add a new expense          (AddExpense.kt)
GET    /expenses/get_all_expenses                  → get all expenses (limit 50)(HistoryScreen)
GET    /expenses/today            → today's expenses only      (HomeScreen)
GET    /expenses/search           → search + category filter   (SearchDialog)
PATCH  /expenses/{expense_id}     → edit an expense            (DetailScreen edit icon)
DELETE /expenses/{expense_id}     → delete an expense          (DetailScreen delete button)
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from typing import Literal, Optional, Union
from db import get_db
from auth.auth import get_current_user
from schemas.expense_schema import (
    ExpenseCreate,
    ExpenseUpdate,
    ExpenseResponse,
    ExpenseListResponse,
    VALID_CATEGORIES,
)
from crud.expense_crud import (
    add_expense,
    get_all_expenses,
    get_today_expenses,
    search_expenses,
    edit_expense,
    delete_expense,
)
# from cache import cache_delete, home_key

router = APIRouter(prefix="/expenses", tags=["expenses"])


# ── Add expense ────────────────────────────────────────────────────────────────

@router.post(
    "/add_expense",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new expense",
    description="""
Called when the user taps **Add Expense** in `AddExpense.kt`.

- `amount`  must be between **0.01** and **2,00,000** (₹ cap from the frontend).
- `category` must be one of: `Food | Transport | Shopping | Leisure | Housing | Health | Education | Other`
  (case-insensitive — `"food"` is accepted and normalised to `"Food"`).
- `contact_name` and `contact_number` are optional (user may skip the contact picker).
- `date` should be an ISO-8601 UTC timestamp (`"2026-05-20T13:30:00Z"`).

Example request:
```json
{
    "amount": 450.00,
    "category": "Food",
    "notes": "Lunch at Saravana Bhavan",
    "date": "2026-05-20T13:30:00Z",
    "contact_name": "Rahul Kumar",
    "contact_number": "9876543210"
}
```

Example response:
```json
{
    "id": 42,
    "user_id": 7,
    "amount": 450.0,
    "category": "Food",
    "notes": "Lunch at Saravana Bhavan",
    "date": "20 May 2026",
    "time": "01:30 PM",
    "contact_name": "Rahul Kumar",
    "contact_number": "9876543210",
    "created_at": "2026-05-20T13:30:00Z",
    "updated_at": "2026-05-20T13:30:00Z"
}
```
""",
)
async def add_expense_route(
    payload      : ExpenseCreate,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    result = await add_expense(db, current_user["id"], payload)  # DB write first
    # await cache_delete(home_key(current_user["id"]))              # then invalidate
    return result


# ── Get all expenses (limit 50) ────────────────────────────────────────────────

@router.get(
    "/get_all_expenses",
    response_model=ExpenseListResponse,
    summary="Get all expenses — newest first, default limit 50",
    description="""
Used by **HistoryScreen** to populate the `Expense History` tab.

Results are sorted **newest first** (`created_at DESC`).
Default limit is **50** to match `TransactionRow` list rendering;
increase via `?limit=` and paginate with `?offset=`.

`total` in the response is the grand total count across all pages —
use it for "Showing 50 of 120" labels.

Example response:
```json
{
    "total": 120,
    "expenses": [
        {
            "id": 42,
            "amount": 450.0,
            "category": "Food",
            "date": "20 May 2026",
            "time": "01:30 PM",
            ...
        }
    ]
}
```
""",
)
async def get_all_expenses_route(
    limit        : int          = Query(50,  ge=1, le=200, description="Max rows to return (default 50)"),
    offset       : int          = Query(0,   ge=0,         description="Rows to skip for pagination"),
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    total, expenses = await get_all_expenses(db, current_user["id"], limit, offset)
    return ExpenseListResponse(total=total, expenses=expenses)


# ── Today's expenses ───────────────────────────────────────────────────────────

@router.get(
    "/today",
    response_model=ExpenseListResponse,
    summary="Get today's expenses — used by HomeScreen DailySummaryCard",
    description="""
Returns all expenses whose timestamp falls on **today** (server UTC date).

Used by `HomeScreen.kt`:
- **DailySummaryCard** → `Spent Today` amount (sum the `amount` fields)
- **TodayExpensesSection** → the 3-card recent expense list

`total` is the count for today only.

Example response:
```json
{
    "total": 3,
    "expenses": [
        { "id": 42, "amount": 45.0,  "category": "Food",      "time": "01:30 PM", ... },
        { "id": 41, "amount": 15.0,  "category": "Transport", "time": "10:00 AM", ... },
        { "id": 40, "amount": 25.0,  "category": "Shopping",  "time": "09:15 AM", ... }
    ]
}
```
""",
)
async def get_today_expenses_route(
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    total, expenses = await get_today_expenses(db, current_user["id"])
    return ExpenseListResponse(total=total, expenses=expenses)


# ── Search expenses ────────────────────────────────────────────────────────────

@router.get(
    "/search",
    response_model=ExpenseListResponse,
    summary="Search expenses by keyword or category",
    description=f"""
Triggered from the **search icon** in `HistoryScreen.kt` (opens `SearchDialog`).

Query parameters:
- `q`        — case-insensitive substring match on **notes**, **contact_name**, or **category**
- `category` — exact category filter. Allowed values: `{sorted(VALID_CATEGORIES)}`
- `limit`    — default 50 (same as GET /expenses)
- `offset`   — for pagination

Both `q` and `category` are optional and can be combined.

Example — search notes for "lunch":
```
GET /expenses/search?q=lunch
```

Example — all Food expenses:
```
GET /expenses/search?category=Food
```

Example — food + keyword:
```
GET /expenses/search?q=saravana&category=Food
```

Example response:
```json
{{
    "total": 4,
    "expenses": [ {{ ... }}, ... ]
}}
```
""",
)
async def search_expenses_route(
    q            : Optional[str] = Query(None, description="Keyword to search in notes / contact / category"),
    category     : Optional[str] = Query(None, description="Exact category filter"),
    limit        : int           = Query(50, ge=1, le=200),
    offset       : int           = Query(0,  ge=0),
    db           : AsyncSession  = Depends(get_db),
    current_user : dict          = Depends(get_current_user),
):
    total, expenses = await search_expenses(
        db, current_user["id"], query=q, category=category,
        limit=limit, offset=offset,
    )
    return ExpenseListResponse(total=total, expenses=expenses)


# ── Edit expense ───────────────────────────────────────────────────────────────

@router.patch(
    "/{expense_id}",
    response_model=ExpenseResponse,
    summary="Edit an expense — PATCH semantics, only sent fields are updated",
    description="""
Triggered from the **edit icon** (pencil) in `DetailScreen.kt`.

All fields are optional — only the fields you include in the request body
will be updated. Omit fields you do not want to change.

Raises **404** if the expense does not exist or belongs to a different user.
Raises **422** if the request body is completely empty.

Example — update only the amount:
```json
{ "amount": 500.00 }
```

Example — update category and notes:
```json
{
    "category": "Transport",
    "notes": "Ola cab, not Uber"
}
```

Example — update everything:
```json
{
    "amount": 120.00,
    "category": "Transport",
    "notes": "Cab to airport",
    "date": "2026-05-20T18:00:00Z",
    "contact_name": "Driver Suresh",
    "contact_number": "9123456789"
}
```
""",
)
async def edit_expense_route(
    expense_id   : int,
    payload      : ExpenseUpdate,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    result = await edit_expense(db, current_user["id"], expense_id, payload)
    # await cache_delete(home_key(current_user["id"]))
    return result


# ── Delete expense ─────────────────────────────────────────────────────────────

@router.delete(
    "/{expense_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete an expense",
    description="""
Triggered from the red **Delete Transaction** button in `DetailScreen.kt`.

After a successful delete, the app navigates back to `HistoryScreen`
(`navController.navigate("history")`).

Raises **404** if the expense does not exist or belongs to a different user.

Example response:
```json
{ "message": "Expense deleted successfully", "deleted_id": 42 }
```
""",
)
async def delete_expense_route(
    expense_id   : int,
    db           : AsyncSession = Depends(get_db),
    current_user : dict         = Depends(get_current_user),
):
    await delete_expense(db, current_user["id"], expense_id)
    # await cache_delete(home_key(current_user["id"]))
    return {"message": "Expense deleted successfully", "deleted_id": expense_id}

@router.get(
    "/search",
    summary="Unified history search — routes by tab name",
    description=f"""
Called by `SearchDialog` in `HistoryScreen.kt` when the user submits a search.
 
The `tab` parameter tells the server which dataset to search:
 
**tab=expenses**
- Searches `notes`, `contact_name`, and `category` (case-insensitive substring).
- Optional `category` filter restricts to an exact category.
- Allowed categories: `{sorted(VALID_CATEGORIES)}`
- Returns `ExpenseListResponse` shape.
 
**tab=budget**
- Searches task names inside archived week `tasks_data` JSON.
- Also matches on week date range strings (e.g. "May 2026").
- Returns `HistoryListResponse` shape.
 
Both responses include a `total` count for pagination labels.
 
Examples:
```
GET /history/search?tab=expenses&q=lunch
GET /history/search?tab=expenses&q=saravana&category=Food
GET /history/search?tab=budget&q=groceries
GET /history/search?tab=budget&q=2026-05
```
""",
    # FastAPI can't declare a true discriminated union as response_model,
    # so we leave it as the default (dict) and document shapes above.
    status_code=status.HTTP_200_OK,
)
async def unified_search(
    tab          : Literal["expenses", "budget"] = Query(
                       ...,
                       description="Which history tab is active: 'expenses' or 'budget'",
                   ),
    q            : Optional[str] = Query(None,  description="Free-text keyword to search"),
    category     : Optional[str] = Query(None,  description="Expense category filter (expenses tab only)"),
    limit        : int           = Query(50,  ge=1, le=200, description="Max results (default 50)"),
    offset       : int           = Query(0,   ge=0,         description="Rows to skip for pagination"),
    db           : AsyncSession  = Depends(get_db),
    current_user : dict          = Depends(get_current_user),
) -> dict:
 
    user_id = current_user["id"]
 
    # ── Expenses tab ───────────────────────────────────────────────────────────
    if tab == "expenses":
        total, expenses = await search_expenses(
            db,
            user_id,
            query    = q,
            category = category,
            limit    = limit,
            offset   = offset,
        )
        return {
            "tab"      : "expenses",
            "total"    : total,
            "expenses" : [e.model_dump() for e in expenses],
        }
 
    # ── Budget tab ─────────────────────────────────────────────────────────────
    # Fetch all history rows for the user (up to limit+offset for pagination),
    # then filter in Python by task name or week date string.
    # Budget history is at most ~52 rows/year so in-Python scan is fine.
    else:
        # Pull enough rows to apply the search filter with pagination.
        # We fetch a generous ceiling (max 52 weeks = 1 year) then slice.
        result = await get_history(db, user_id, limit=52, offset=0)
        all_weeks = result.weeks
 
        if q:
            keyword = q.strip().lower()
            filtered = []
            for week in all_weeks:
                # Match on week date strings
                if (
                    keyword in str(week.week_start).lower()
                    or keyword in str(week.week_end).lower()
                ):
                    filtered.append(week)
                    continue
 
                # Match on any task name inside tasks_data
                matched = False
                for day_tasks in week.tasks_data.values():
                    for task in day_tasks:
                        if keyword in task.get("name", "").lower():
                            matched = True
                            break
                    if matched:
                        break
                if matched:
                    filtered.append(week)
        else:
            filtered = all_weeks
 
        total         = len(filtered)
        paged         = filtered[offset : offset + limit]
 
        return {
            "tab"   : "budget",
            "total" : total,
            "weeks" : [w.model_dump() for w in paged],
        }
