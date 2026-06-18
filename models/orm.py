"""SQLAlchemy ORM models for the analytics platform."""

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    String,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

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

class PriceImpactCheck(Base):
    __tablename__ = 'price_impact_checks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String, nullable=False, index=True)
    market_id = Column(String, nullable=False)
    asset_id = Column(String, nullable=False)
    
    direction = Column(String, nullable=False)            # "BUY" or "SELL"
    entry_price = Column(Float, nullable=False)
    entry_time = Column(Float, nullable=False)            # Epoch timestamp
    
    checkpoint_interval = Column(String, nullable=False)  # "5m", "15m", "1h", "4h", "24h"
    target_check_time = Column(Float, nullable=False, index=True) # entry_time + offset_seconds
    
    check_price = Column(Float, nullable=True)
    price_change_pct = Column(Float, nullable=True)
    is_completed = Column(Boolean, default=False, nullable=False, index=True)