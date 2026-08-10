import os
import logging
from dotenv import load_dotenv
from contextlib import contextmanager
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

logger = logging.getLogger("polymarket.api")

# httpx (the Telegram bot client's HTTP library) logs full request URLs at
# INFO level, and Telegram's Bot API embeds the bot token directly in the
# URL path (https://api.telegram.org/bot<TOKEN>/sendMessage) rather than a
# header -- confirmed live that this leaked a real BOT_TOKEN into container
# logs on every Telegram API call. Set explicitly here (not via
# logging.basicConfig) so it holds regardless of import order or what any
# other module's basicConfig call configures for the root logger --
# Logger.setLevel() on a specific logger always wins over an inherited root
# level. db.py is imported early by every entry point (app.py, scripts),
# so this takes effect before any Telegram/HTTP call can happen.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
        ("price_impact_checks", "anomaly_event_id", "INTEGER"),
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


def normalize_onchain_trade_market_ids() -> None:
    """Best-effort idempotent data self-heal for existing OnchainTrade rows.

    The Dune-based wallet-intelligence ingestion query used to SELECT
    market_id as raw to_hex(condition_id) -- 64 uppercase hex characters,
    no 0x prefix -- while AutoSubscription.condition_id (Gamma's format)
    is lowercase and 0x-prefixed. That mismatch was fixed at the SELECT
    level (core/wallet_intelligence_scheduler.py), which stops the
    problem for new ingestions -- but rows already written before that
    fix landed keep their old, un-normalized market_id forever: the
    per-row backfill path in _ingest_rows only fires when a row's
    category is NULL, and every row from a completed Dune ingestion
    already has one set. Without this, market_insider_risk /
    build_signal2_score's `OnchainTrade.market_id == condition_id` filter
    keeps finding zero rows for exactly the historical data that
    motivated the SELECT-level fix in the first place.

    A plain UPDATE (not a per-row Python loop) since this can touch a
    large number of rows in one pass; guarded to only match Dune's raw
    format (64 hex chars, no 0x prefix) so it never touches the Polygon
    live monitor's rows, which store a decimal ERC1155 token_id in this
    same column (a different, unrelated placeholder format -- see
    blockchain/polygon_sync.py's _process_batch). `NOT LIKE '0x%' AND
    length(...) = 64` is deliberately portable SQL (no regex operator)
    so this runs correctly on both SQLite (tests) and Postgres (prod).
    The WHERE guard matches zero rows once fully normalized, so this is
    cheap and safe to run unconditionally on every startup, matching
    ensure_additive_columns' pattern.
    """
    try:
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE onchain_trades SET market_id = '0x' || lower(market_id) "
                "WHERE market_id NOT LIKE '0x%' AND length(market_id) = 64"
            ))
            if result.rowcount:
                logger.info(f"Normalized market_id format for {result.rowcount} existing OnchainTrade row(s)")
    except Exception as e:
        logger.warning(f"OnchainTrade market_id normalization failed (best-effort, continuing): {e}")


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