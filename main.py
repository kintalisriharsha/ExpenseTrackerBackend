from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import logging
import asyncio
import uvicorn
import os

from api_routes.auth_route import router as auth_router
from db import create_tables, warmup_connection_pool, close_db, check_db_health

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()


# ── Background task: clean up expired blacklist rows ──────────────────────────

async def _cleanup_loop(interval_seconds: int = 3600) -> None:
    """
    Runs every `interval_seconds` (default 1 hour).
    Deletes blacklisted token rows whose expiry has passed.
    These can never be used again anyway, so it's safe to purge them.
    """
    from db import AsyncSessionLocal
    from crud.auth_crud import cleanup_expired_blacklist

    while True:
        await asyncio.sleep(interval_seconds)
        async with AsyncSessionLocal() as db:
            try:
                await cleanup_expired_blacklist(db)
                await db.commit()
            except Exception as e:
                logger.error(f"Blacklist cleanup task failed: {e}")


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────────────
    logger.warning("Starting Expense Tracker API...")
    await create_tables()
    await warmup_connection_pool()

    # Start background cleanup task
    cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.warning("Startup complete")

    yield

    # ── Shutdown ───────────────────────────────────────────────────────
    logger.warning("Shutting down...")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await close_db()
    logger.warning("Shutdown complete")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "Expense Tracker API",
    description = "Backend API for the Expense Tracker Android app",
    version     = "1.0.0",
    lifespan    = lifespan,
    # redirect_slashes=False is intentional:
    # keeps URLs canonical and avoids surprises with some Retrofit configs.
    redirect_slashes = False,
)


# ── Middleware ─────────────────────────────────────────────────────────────────

# CORS is not needed by the Android app itself (Retrofit is not a browser).
# Uncomment when you add a web dashboard.  Never use allow_origins=["*"] in
# production — list your actual frontend domain(s) explicitly.
#
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins     = ["https://your-dashboard.example.com"],
#     allow_methods     = ["GET", "POST", "PUT", "PATCH", "DELETE"],
#     allow_headers     = ["Authorization", "Content-Type"],
#     allow_credentials = True,
# )

# HTTPS redirect — uncomment on production (Render / Railway handle TLS for you
# so this is usually not needed there; only enable if you manage your own nginx).
# from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
# app.add_middleware(HTTPSRedirectMiddleware)


# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(auth_router)

# Uncomment as you build each feature:
# from routers.expense_router import router as expense_router
# from routers.budget_router  import router as budget_router
# from routers.goals_router   import router as goals_router
# from routers.profile_router import router as profile_router
# app.include_router(expense_router)
# app.include_router(budget_router)
# app.include_router(goals_router)
# app.include_router(profile_router)


# ── Root ───────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "message": "Expense Tracker API",
        "version": "1.0.0",
        "docs":    "/docs",
    }


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Returns DB connection status and pool stats.
    Used by Render / Railway for liveness checks.
    """
    db_health = await check_db_health()
    return {
        "api":      "healthy",
        "database": db_health,
    }


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host      = "0.0.0.0",
        port      = 8000,
        reload    = True,
        log_level = "info",
    )
