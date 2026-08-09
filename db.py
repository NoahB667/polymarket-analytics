"""Re-export from core.db for backward compatibility."""

from core.db import (
    engine,
    SessionLocal,
    Base,
    get_db_session,
    ensure_additive_columns,
    normalize_onchain_trade_market_ids,
    logger,
)

__all__ = [
    "engine",
    "SessionLocal",
    "Base",
    "get_db_session",
    "ensure_additive_columns",
    "normalize_onchain_trade_market_ids",
    "logger",
]
