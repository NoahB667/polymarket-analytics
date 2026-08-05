import os
import logging
from dotenv import load_dotenv
from contextlib import contextmanager
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

logger = logging.getLogger("polymarket.api")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.critical("DATABASE_URL environment variable is not set!")
    raise ValueError("DATABASE_URL must be configured.")

logger.info(f"Connecting to database: {DATABASE_URL.split('@')[-1]}") # Masking credentials

engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600 
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def ensure_additive_columns() -> None:
    """Best-effort idempotent column migrations for existing tables.

    This project has no migration tool (no Alembic anywhere in the repo).
    `Base.metadata.create_all()` only creates missing TABLES -- it never
    alters an existing one. Confirmed live: adding
    WalletProfile.score_stale (for the Polygon live monitor) was invisible
    against a dev database whose wallet_profiles table predated the
    column, causing every write to fail with UndefinedColumn until this
    ran. Call this right after Base.metadata.create_all(bind=engine).

    Checks column existence via SQLAlchemy's inspector first, then runs a
    plain `ADD COLUMN` -- deliberately not `ADD COLUMN IF NOT EXISTS`,
    which is Postgres-only syntax SQLite's ALTER TABLE doesn't support,
    making the inspect-first approach both portable (testable on SQLite,
    correct on this project's real Postgres target) and safe to run
    unconditionally on every startup. Each migration gets its own
    transaction so one failure doesn't block the rest.
    """
    migrations = [
        ("wallet_profiles", "score_stale", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ]
    inspector = inspect(engine)
    for table_name, column_name, column_def in migrations:
        try:
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
            if column_name in existing_columns:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}"))
        except Exception as e:
            logger.warning(f"Additive column migration failed (best-effort, continuing): {table_name}.{column_name}: {e}")


@contextmanager
def get_db_session():
    """Provides a thread-safe, isolated database session context."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()