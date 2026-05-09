from fastapi import FastAPI
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import logging
import uvicorn
import os

from db import create_tables, warmup_connection_pool, close_db, check_db_health

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

# ── Lifespan ───────────────────────────────────────────────────────────────────
# Runs once on startup → yields → runs once on shutdown
# Replaces the old @app.on_event("startup") pattern

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────────────
    logger.warning("Starting Expense Tracker API...")
    await create_tables()
    await warmup_connection_pool()
    logger.warning("Startup complete")

    yield   # app is running

    # ── Shutdown ───────────────────────────────────────────────────────
    logger.warning("Shutting down Expense Tracker API...")
    await close_db()
    logger.warning("Shutdown complete")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Expense Tracker API",
    description="Backend API for the Expense Tracker Android app",
    version="1.0.0",
    lifespan=lifespan,
    redirect_slashes=False,
)

# ── Middleware ─────────────────────────────────────────────────────────────────

# CORS — commented out for now (Android Retrofit does not need CORS)
# Uncomment if you add a web dashboard later
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# HTTPS redirect — uncomment on production deployment only
# Keeping this off locally so uvicorn --reload works over http://
# app.add_middleware(HTTPSRedirectMiddleware)

# ── Routers ────────────────────────────────────────────────────────────────────
# Uncomment each router as you build it

# from routers.auth_router       import router as auth_router
# from routers.expense_router    import router as expense_router
# from routers.budget_router     import router as budget_router
# from routers.goals_router      import router as goals_router
# from routers.profile_router    import router as profile_router

# app.include_router(auth_router)
# app.include_router(expense_router)
# app.include_router(budget_router)
# app.include_router(goals_router)
# app.include_router(profile_router)

# ── Root ───────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Expense Tracker API",
        "version": "1.0.0",
        "docs":    "/docs",
    }

# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Health check endpoint.
    Returns DB connection status and pool stats.
    Useful for deployment platforms (Render, Railway) to verify the app is alive.
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
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )