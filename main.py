"""
main.py  (Circuit Breaker + DB fallback)
─────────────────────────────────────────
/health now reports circuit breaker state so you can monitor Redis health
without connecting to Redis directly.
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

from cache import get_redis, close_redis, get_circuit_breaker

logging.basicConfig(level=logging.WARNING, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.warning("Starting Expense Tracker API...")
    await create_tables()
    await warmup_connection_pool()
    await get_redis()   # warm up Redis + initial circuit breaker state
    logger.warning("Startup complete")
    yield
    logger.warning("Shutting down...")
    await close_db()
    await close_redis()
    logger.warning("Shutdown complete")


app = FastAPI(
    title        = "Expense Tracker API",
    version      = "1.0.0",
    lifespan     = lifespan,
    redirect_slashes = False,
    docs_url     = None,
    redoc_url    = None,
    openapi_url  = None,
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
    cb = get_circuit_breaker()

    return {
        "api"      : "healthy",
        "database" : db_health,
        "redis"    : {
            # Circuit breaker state is the authoritative Redis health signal.
            # CLOSED = healthy, OPEN = degraded (using DB fallback), HALF_OPEN = recovering
            **cb.status_dict(),
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