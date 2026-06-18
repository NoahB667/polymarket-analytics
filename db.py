import os
import logging
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger("polymarket.api")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///polymarket.db")

engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@contextmanager
def get_db_session():
    """Provides a thread-safe, isolated database session context."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()