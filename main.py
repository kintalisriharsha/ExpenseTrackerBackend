"""
main.py
───────
Startup / shutdown lifecycle, routers, health check, and cron endpoints.
"""

from fastapi import FastAPI, Depends
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import logging
import uvicorn
import os

from api_routes.auth_route      import router as auth_router
from api_routes.setting_route   import router as setting_router
from api_routes.expense_route   import router as expense_router
from api_routes.goal_route      import router as goal_router
from api_routes.budget_route    import router as budget_router
from api_routes.home_route      import router as home_router
from api_routes.analytics_route import router as analytics_router

from db import create_tables, warmup_connection_pool, close_db, check_db_health
from sqlalchemy.ext.asyncio import AsyncSession
from db import get_db
from auth.auth import verify_cron_secret
from crud.setting_crud import carry_forward_all_users
from crud.budget_crud  import rollover_all_users

# cache.py exports: _get_redis (private), close_redis (no-op), no circuit breaker
from cache import close_redis

logging.basicConfig(level=logging.WARNING, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.warning("Starting Expense Tracker API...")
    await create_tables()
    await warmup_connection_pool()
    # cache.py uses a lazy singleton — no explicit warmup needed;
    # the first real cache call will initialise the Upstash HTTP client.
    logger.warning("Startup complete")
    yield
    logger.warning("Shutting down...")
    await close_db()
    await close_redis()   # no-op for Upstash HTTP client, kept for symmetry
    logger.warning("Shutdown complete")


app = FastAPI(
    title            = "Expense Tracker API",
    version          = "1.0.0",
    lifespan         = lifespan,
    redirect_slashes = False,
    docs_url         = None,
    redoc_url        = None,
    openapi_url      = None,
)

app.include_router(auth_router)
app.include_router(setting_router)
app.include_router(expense_router)
app.include_router(goal_router)
app.include_router(budget_router)
app.include_router(home_router)
app.include_router(analytics_router)


@app.get("/")
async def root():
    return {"message": "Expense Tracker API", "version": "1.0.0"}


@app.get("/health")
async def health():
    db_health = await check_db_health()
    # cache.py has no circuit breaker — report Redis as configured/not-configured
    # based solely on whether the env vars are present.
    redis_url   = os.getenv("REDIS_URL")
    redis_token = os.getenv("REDIS_TOKEN")
    redis_status = "configured" if (redis_url and redis_token) else "disabled (env vars missing)"

    return {
        "api"      : "healthy",
        "database" : db_health,
        "redis"    : {
            "status": redis_status,
        },
    }


@app.post("/cron/carry-forward-all", tags=["cron"])
async def cron_carry_forward(
    db: AsyncSession = Depends(get_db),
    _:  None         = Depends(verify_cron_secret),
):
    return await carry_forward_all_users(db)


@app.post("/cron/rollover-all", tags=["cron"])
async def cron_rollover(
    db: AsyncSession = Depends(get_db),
    _:  None         = Depends(verify_cron_secret),
):
    return await rollover_all_users(db, carry_forward_budget=True)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True, log_level="info")
