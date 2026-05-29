from fastapi import FastAPI
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

logging.basicConfig(
    level  = logging.WARNING,
    format = "%(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.warning("Starting Expense Tracker API...")
    await create_tables()
    await warmup_connection_pool()
    logger.warning("Startup complete")

    yield

    logger.warning("Shutting down...")
    await close_db()
    logger.warning("Shutdown complete")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "Expense Tracker API",
    description = "Backend API for the Expense Tracker Android app",
    version     = "1.0.0",
    lifespan    = lifespan,
    redirect_slashes = False,
    # Disable public docs in production — remove these lines locally if needed
    docs_url    = None,
    redoc_url   = None,
    openapi_url = None,
)


# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(setting_router)
app.include_router(expense_router)
app.include_router(goal_router)
app.include_router(budget_router)
app.include_router(home_router)
app.include_router(analytics_router)


# ── Health & root ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"message": "Expense Tracker API", "version": "1.0.0"}


@app.get("/health")
async def health():
    db_health = await check_db_health()
    return {"api": "healthy", "database": db_health}


# ── Entry point ────────────────────────────────────────────────────────────────
# Cloud Run injects the PORT environment variable (default 8080).
# Local dev falls back to 8000.

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "main:app",
        host      = "0.0.0.0",
        port      = port,
        reload    = True,
        log_level = "info",
    )
