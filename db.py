from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy import text
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# ── Load environment ───────────────────────────────────────────────────────────

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set in .env")

# ── Convert URL to asyncpg format ──────────────────────────────────────────────
# Neon gives you  postgresql://  or  postgres://
# asyncpg needs   postgresql+asyncpg://

if DATABASE_URL.startswith("postgresql://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
else:
    raise ValueError("DATABASE_URL must start with postgresql:// or postgres://")

# asyncpg does not accept sslmode in the URL — strip it out
# SSL is handled in connect_args instead
if "?" in ASYNC_DATABASE_URL:
    ASYNC_DATABASE_URL = ASYNC_DATABASE_URL.split("?")[0]

# ── Engine ─────────────────────────────────────────────────────────────────────
# pool_size=10        → keep 10 connections warm at all times
# max_overflow=20     → allow 20 extra on demand  (total max = 30)
# pool_timeout=30     → wait max 30s for a free connection
# pool_recycle=1800   → recycle connections every 30 min (Neon serverless safe)
# pool_pre_ping=True  → run SELECT 1 before handing out a connection
# pool_use_lifo=True  → reuse recently used connections first (better for serverless)

async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    connect_args={
        "ssl":              "require",          # Neon requires SSL
        "timeout":          10,                 # connection timeout seconds
        "command_timeout":  30,                 # query timeout seconds
        "statement_cache_size": 100,            # cache 100 prepared statements
        "server_settings": {
            "application_name": "expense_tracker_api",
            "jit":              "off"           # disable JIT — faster for short queries
        }
    },
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    pool_use_lifo=True,
    echo=False,         # set True locally to log every SQL query
    future=True,
)

# ── Session factory ────────────────────────────────────────────────────────────

AsyncSessionLocal = sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,     # keep ORM objects usable after commit
    autoflush=False,            # manual flush control — better for async
    autocommit=False,
)

# ── Declarative base ───────────────────────────────────────────────────────────
# All models import Base from here and call Base.metadata.create_all()

class Base(DeclarativeBase):
    pass

# ── FastAPI dependency ─────────────────────────────────────────────────────────
# Usage in any router:
#   db: AsyncSession = Depends(get_db)

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            logger.error(f"DB session error: {e}")
            raise
        else:
            try:
                await session.commit()
            except Exception as e:
                logger.error(f"DB commit error: {e}")
                await session.rollback()
                raise

# ── Lifespan helpers ───────────────────────────────────────────────────────────
# Called from main.py lifespan context manager

async def create_tables() -> None:
    """Create all tables on startup if they don't exist."""
    try:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.warning("Tables created / verified successfully")
    except Exception as e:
        logger.error(f"Failed to create tables: {e}")
        raise


async def warmup_connection_pool() -> None:
    """
    Pre-warm the connection pool at startup.
    Opens 5 connections silently so the first real requests don't pay
    the cold-start cost of establishing a new Neon connection.
    """
    try:
        tasks = [_warm_single_connection() for _ in range(5)]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.warning("Connection pool warmed up")
    except Exception:
        pass    # warmup is optional — never block startup


async def _warm_single_connection() -> None:
    """Open one connection, run SELECT 1, release back to pool."""
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pass


async def close_db() -> None:
    """Dispose all pool connections on app shutdown."""
    try:
        await async_engine.dispose()
        logger.warning("Database connections closed")
    except Exception as e:
        logger.error(f"Error closing DB connections: {e}")


# ── Health check helper ────────────────────────────────────────────────────────
# Used by GET /health endpoint in main.py

async def check_db_health() -> dict:
    import time
    start = time.time()
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        elapsed = round((time.time() - start) * 1000, 2)
        return {
            "status":           "healthy",
            "response_time_ms": elapsed,
            "pool_size":        async_engine.pool.size(),
            "pool_checked_out": async_engine.pool.checkedout(),
        }
    except Exception as e:
        elapsed = round((time.time() - start) * 1000, 2)
        return {
            "status":           "unhealthy",
            "error":            str(e),
            "response_time_ms": elapsed,
        }