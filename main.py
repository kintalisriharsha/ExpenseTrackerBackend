from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import logging
import uvicorn
import os
from api_routes.auth_route import router as auth_router
from api_routes.setting_route import router as setting_router
from db import create_tables, warmup_connection_pool, close_db, check_db_health
from api_routes.expense_route import router as expense_router

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────────────
    logger.warning("Starting Expense Tracker API...")
    await create_tables()           # create DB tables if not exist
    await warmup_connection_pool()  # warm up asyncpg connection pool
    logger.warning("Startup complete")

    yield   # app is running

    # ── Shutdown ───────────────────────────────────────────────────────
    logger.warning("Shutting down...")
    await close_db()
    logger.warning("Shutdown complete")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Expense Tracker API",
    description="Backend API for the Expense Tracker Android app",
    version="1.0.0",
    lifespan=lifespan,
    redirect_slashes=False,
    docs_url=None,      # blocks /docs
    redoc_url=None,     # blocks /redoc
    openapi_url=None,   # blocks /openapi.json
)

# ── Middleware ─────────────────────────────────────────────────────────────────

# CORS — commented out (Android Retrofit does not need CORS)
# Uncomment if you add a web dashboard later
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# HTTPS redirect — uncomment on production only
# app.add_middleware(HTTPSRedirectMiddleware)

# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(auth_router)

app.include_router(setting_router)

app.include_router(expense_router)

# ── Root ───────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Expense Tracker API",
        "version": "1.0.0",
    }

# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Health check — returns DB connection status and pool stats.
    Used by Render / Railway to verify the app is alive.
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