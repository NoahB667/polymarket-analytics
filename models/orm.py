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
from db import Base


class Subscription(Base):
    """Tracks active Telegram subscriptions by chat and market slug."""

    __tablename__ = "subscription"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(50), nullable=False)
    slug = Column(String(200), nullable=False)
    limit_usd = Column(Float, default=0.0)

    __table_args__ = (UniqueConstraint("chat_id", "slug", name="_chat_slug_uc"),)


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

    __table_args__ = (Index("idx_trade_slug_timestamp", "slug", "timestamp"),)


class PriceImpactCheck(Base):
    __tablename__ = "price_impact_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String, nullable=False, index=True)
    market_id = Column(String, nullable=False)
    asset_id = Column(String, nullable=False)

    direction = Column(String, nullable=False)  # "BUY" or "SELL"
    entry_price = Column(Float, nullable=False)
    entry_time = Column(Float, nullable=False)  # Epoch timestamp

    checkpoint_interval = Column(
        String, nullable=False
    )  # "5m", "15m", "1h", "4h", "24h"
    target_check_time = Column(
        Float, nullable=False, index=True
    )  # entry_time + offset_seconds

    check_price = Column(Float, nullable=True)
    price_change_pct = Column(Float, nullable=True)
    is_completed = Column(Boolean, default=False, nullable=False, index=True)


class OnchainTrade(Base):
    """Historical trade transactions pulled from Dune's Polygon data lake tables."""

    __tablename__ = "onchain_trades"

    blockchain_id = Column(String, primary_key=True, index=True)
    wallet_address = Column(String, nullable=False, index=True)
    market_id = Column(String, nullable=False, index=True)
    slug = Column(String, nullable=True, index=True)
    question = Column(String, nullable=True)
    outcome = Column(String, nullable=True)
    usd_volume = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)  # Implied probability (0.0 to 1.0)
    resolved_outcome = Column(String, nullable=True)  # Populated when market settles
    realized_pnl = Column(Float, nullable=True)  # Realized profit/loss in USDC
    block_timestamp = Column(Float, nullable=False)  # Epoch float timestamp
    category = Column(
        String, nullable=True, index=True
    )  # Coarse topic sector, from event_market_name
    market_end_time = Column(
        Float, nullable=True
    )  # Epoch float; market's end_date_iso from CLOB


class WalletProfile(Base):
    __tablename__ = "wallet_profiles"

    wallet_address = Column(String, primary_key=True, index=True)
    total_trades = Column(Integer, default=0, nullable=False)
    distinct_markets = Column(Integer, default=0, nullable=False)

    long_shot_attempts = Column(
        Integer, default=0, nullable=False
    )  # Bets placed at entry_price <= 0.20
    long_shot_wins = Column(Integer, default=0, nullable=False)  # Long-shots that hit
    long_shot_win_rate = Column(Float, default=0.0, nullable=False)

    category_concentration = Column(
        Float, default=0.0, nullable=False
    )  # % of trades in dominant sector
    account_age_days = Column(Float, default=0.0, nullable=False)
    average_position_size = Column(Float, default=0.0, nullable=False)

    avg_implied_prob_at_entry = Column(
        Float, default=0.0, nullable=False
    )  # Baseline win-rate expectation
    avg_days_before_resolution = Column(
        Float, default=0.0, nullable=False
    )  # Avg gap between trade and market close
    new_account_flag = Column(Boolean, default=False, nullable=False)
    top_categories = Column(
        String, nullable=True
    )  # Comma-joined top sectors, most-common first

    insider_score = Column(
        Float, default=0.0, nullable=False, index=True
    )  # Final rating (0.0 to 1.0)
    last_updated = Column(Float, nullable=False)
