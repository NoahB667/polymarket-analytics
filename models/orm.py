"""SQLAlchemy ORM models for the analytics platform."""

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    JSON,
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
    anomaly_event_id = Column(Integer, nullable=True, index=True)

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


class AnomalyEvent(Base):
    """Append-only log of every generated AnomalyEvent (surveillance product).

    Never UPDATE or DELETE rows here (PROJECT_CONTEXT.md rule 5) -- every
    detected anomaly is stored permanently for private analysis, same
    append-only discipline as the `signal` table.
    """

    __tablename__ = "anomaly_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String(100), nullable=False, index=True)
    slug = Column(String(200), nullable=False, index=True)
    question = Column(String(500))
    category = Column(String(500))
    timestamp = Column(Float, nullable=False, index=True)

    trigger = Column(String(30), nullable=False)
    severity = Column(String(10), nullable=False, index=True)
    anomaly_score = Column(Float, nullable=False)

    current_price = Column(Float, nullable=False)
    price_change_pct = Column(Float, nullable=False)
    ofi_15min = Column(Float, nullable=False)
    volume_spike_ratio = Column(Float, nullable=False)
    is_long_shot = Column(Boolean, nullable=False)
    buy_pressure_pct = Column(Float, nullable=False)

    anomalous_wallet_count = Column(Integer, nullable=False, default=0)
    market_insider_risk = Column(Float, nullable=False, default=0.0)
    wallet_context_available = Column(Boolean, nullable=False, default=False)

    broadcast_free = Column(Boolean, nullable=False, default=False)
    broadcast_premium = Column(Boolean, nullable=False, default=False)
    broadcast_reason = Column(String(300), nullable=False, default="")

    posted_at_premium = Column(Float, nullable=True)
    posted_at_free = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_anomaly_event_market_timestamp", "market_id", "timestamp"),
        Index("idx_anomaly_event_severity", "severity"),
    )


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

    score_stale = Column(
        Boolean, default=False, nullable=False, index=True
    )  # Set by polygon_sync on new trades; cleared by hourly score recalculation


class AutoSubscription(Base):
    """Tracks markets subscribed to automatically by the discovery scheduler (Step 8.5)."""

    __tablename__ = "auto_subscription"

    id = Column(Integer, primary_key=True)
    slug = Column(String(200), nullable=False, unique=True)
    question = Column(String(500))
    category = Column(String(500))  # joined event tag labels -- can be long (many tags)
    condition_id = Column(String(100), nullable=True)  # Gamma "conditionId", 0x-prefixed hex --
    # the on-chain market identifier Dune's market_trades table keys on. Nullable since
    # rows created before this column existed won't have it until re-discovered.
    market_score = Column(Float, nullable=False)
    tier = Column(Integer, nullable=False)  # 1, 2, or 3 (MIN_ACTIVE_MARKETS floor backfill)
    volume_24h = Column(Float)
    days_remaining = Column(Float)
    token_ids = Column(JSON)  # ["token_id_1", "token_id_2"]
    subscribed_at = Column(Float, nullable=False)  # epoch timestamp
    last_seen_active = Column(Float)
    last_cycle_at = Column(Float)
    status = Column(String(20), default="active", nullable=False)
    # status values: "active", "resolved", "dropped"
    consecutive_misses = Column(Integer, default=0, nullable=False)
    total_trades_collected = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("idx_auto_sub_status", "status"),
        Index("idx_auto_sub_tier", "tier"),
        Index("idx_auto_sub_score", "market_score"),
    )


class PolygonSyncState(Base):
    """Tracks the last Polygon block processed by PolygonSyncService.

    Backup for the Redis `polygon:last_block` key -- read on startup only
    if Redis is empty (e.g. after a Redis flush/restart).
    """

    __tablename__ = "polygon_sync_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    last_block = Column(Integer, nullable=False)
    last_updated = Column(Float, nullable=False)
    events_processed = Column(Integer, default=0, nullable=False)


class Signal(Base):
    """Append-only log of every combined-signal evaluation (Step 10).

    Never UPDATE or DELETE rows here -- backtesting integrity depends on
    this table reflecting exactly what the live system decided at the time
    (CLAUDE.md rule 4).
    """

    __tablename__ = "signal"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String(100), nullable=False, index=True)
    slug = Column(String(200), nullable=False, index=True)
    timestamp = Column(Float, nullable=False, index=True)

    direction = Column(String(10), nullable=False)  # "BUY" or "SELL"
    signal1_confidence = Column(Float, nullable=False)
    signal2_confidence = Column(Float, nullable=False)
    signal2_market_insider_risk = Column(Float, nullable=False)
    combined_score = Column(Float, nullable=False, index=True)
    recommended_action = Column(String(10), nullable=False, index=True)  # TRADE/WATCH/IGNORE
    gates_passed = Column(Boolean, nullable=False)


class PaperPosition(Base):
    """A simulated position opened/closed by the paper trader (Step 10).

    Paper trading only -- never used to place a real order (CLAUDE.md rule 5).
    """

    __tablename__ = "paper_position"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String(100), nullable=False, index=True)
    slug = Column(String(200), nullable=False, index=True)
    asset_id = Column(String(100), nullable=False)

    direction = Column(String(10), nullable=False)  # "BUY" or "SELL"
    entry_price = Column(Float, nullable=False)
    shares = Column(Float, nullable=False)
    cost = Column(Float, nullable=False)
    entry_time = Column(Float, nullable=False, index=True)
    signal_score = Column(Float, nullable=False)

    stop_loss_price = Column(Float, nullable=False)
    take_profit_price = Column(Float, nullable=False)

    status = Column(String(20), nullable=False, default="open", index=True)  # "open" or "closed"
    exit_price = Column(Float, nullable=True)
    exit_time = Column(Float, nullable=True)
    exit_reason = Column(String(20), nullable=True)  # STOP_LOSS/TAKE_PROFIT/RESOLUTION
    pnl = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_paper_position_status", "status"),
        Index("idx_paper_position_market_status", "market_id", "status"),
    )
