import os
import logging
from dotenv import load_dotenv
from contextlib import contextmanager
from sqlalchemy import create_engine
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