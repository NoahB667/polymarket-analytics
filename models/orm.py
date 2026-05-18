"""SQLAlchemy ORM models for the analytics platform."""

from datetime import datetime
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()

_json_type = JSONB().with_variant(JSON, "sqlite")


class Subscription(Base):
    """Tracks active Telegram subscriptions by chat and market slug."""

    __tablename__ = "subscription"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(50), nullable=False)
    slug = Column(String(200), nullable=False)
    limit_usd = Column(Float, default=0.0)

    __table_args__ = (
        UniqueConstraint("chat_id", "slug", name="_chat_slug_uc"),
    )


class Trade(Base):
    """Raw trade data captured from the Polymarket websocket."""

    __tablename__ = "trade"
    id = Column(Integer, primary_key=True)
    slug = Column(String(200), nullable=False, index=True)
    market = Column(String(100))
    asset_id = Column(String(100))
    price = Column(Float)
    size = Column(Float)
    usd = Column(Float)
    side = Column(String(10))
    question = Column(String(500))
    outcome = Column(String(200))
    timestamp = Column(Float, index=True)

    __table_args__ = (
        Index("idx_trade_slug_timestamp", "slug", "timestamp"),
    )
