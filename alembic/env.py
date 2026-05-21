import os
import sys
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from alembic import context

# ── Load .env ──────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# Ensure project root is importable when Alembic is run outside backend cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── Build sync URL from DATABASE_URL in .env ───────────────────────────────────
# .env has:  postgresql://...   (asyncpg format)
# Alembic needs: postgresql+psycopg2://...  (sync format)
_raw_url = os.getenv("DATABASE_URL", "")
if _raw_url.startswith("postgresql://"):
    SYNC_URL = _raw_url.replace("postgresql://", "postgresql+psycopg2://", 1)
elif _raw_url.startswith("postgres://"):
    SYNC_URL = _raw_url.replace("postgres://", "postgresql+psycopg2://", 1)
else:
    raise ValueError("DATABASE_URL not found or has unexpected format in .env")

# ── Alembic config ─────────────────────────────────────────────────────────────
config = context.config

# Override the placeholder sqlalchemy.url in alembic.ini with the real one
config.set_main_option("sqlalchemy.url", SYNC_URL)

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Import your models so autogenerate can detect them ────────────────────────
from db import Base                                  # noqa: E402
from models.user_model import User, BlacklistedToken # noqa: E402
from models.setting_model import Settings
from models.expense_model import Expense
from models.goal_model import Goal

target_metadata = Base.metadata


# ── Offline mode (alembic revision --sql) ─────────────────────────────────────
def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online mode (default) ─────────────────────────────────────────────────────
def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()