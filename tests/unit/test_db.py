import logging
import sys
import time
from pathlib import Path
from unittest.mock import patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

import db
from models.orm import OnchainTrade


def test_httpx_and_httpcore_logging_suppressed_to_prevent_secret_leaks():
    """Regression: httpx logs full request URLs at INFO level, and
    Telegram's Bot API embeds the bot token directly in the URL path
    (https://api.telegram.org/bot<TOKEN>/sendMessage), not a header --
    confirmed live that INFO-level httpx logging leaked a real BOT_TOKEN
    into container logs on every Telegram API call. Must be suppressed at
    import time (module-level, in db.py, imported early by every entry
    point) so it holds regardless of what any other module's
    logging.basicConfig() call later configures for the root logger --
    Logger.setLevel() on a specific logger always wins over an inherited
    root level, independent of call order.
    """
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_ensure_additive_columns_adds_missing_column():
    """Regression test: discovered live that Base.metadata.create_all()
    never alters an existing table, so a new column added to an existing
    model (WalletProfile.score_stale) silently never existed against a
    database whose wallet_profiles table predated it.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE wallet_profiles (wallet_address VARCHAR PRIMARY KEY, total_trades INTEGER)"
        ))

    with patch.object(db, "engine", engine):
        db.ensure_additive_columns()

    columns = {col["name"] for col in inspect(engine).get_columns("wallet_profiles")}
    assert "score_stale" in columns


def test_ensure_additive_columns_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE wallet_profiles (wallet_address VARCHAR PRIMARY KEY, total_trades INTEGER)"
        ))

    with patch.object(db, "engine", engine):
        db.ensure_additive_columns()
        db.ensure_additive_columns()  # must not raise on the second call

    columns = {col["name"] for col in inspect(engine).get_columns("wallet_profiles")}
    assert "score_stale" in columns


def test_ensure_additive_columns_survives_missing_table():
    """If wallet_profiles doesn't exist yet at all (fresh DB before
    create_all ever ran), the ALTER TABLE fails -- must be logged and
    swallowed, not raised, matching this project's best-effort DB rule.
    """
    engine = create_engine("sqlite:///:memory:")

    with patch.object(db, "engine", engine):
        db.ensure_additive_columns()  # must not raise


def test_normalize_onchain_trade_market_ids_normalizes_dune_format_rows():
    """Regression: the SELECT-level market_id fix (core/wallet_intelligence_
    scheduler.py) only stops the mismatch for NEW ingestions -- rows already
    written before that fix landed keep their old format forever, since
    _ingest_rows' per-row backfill only fires on category IS NULL, which is
    never true for a completed Dune ingestion. This self-heal is what
    actually reaches the historical data that motivated the fix.
    """
    engine = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    session.add(OnchainTrade(
        blockchain_id="tx-1", wallet_address="0xwallet",
        market_id="9CB23D04B2DED06147482076688B69B487A8D982C63EBDDA2AB3678CF27CF390",
        usd_volume=100.0, entry_price=0.15, block_timestamp=time.time(),
    ))
    session.commit()
    session.close()

    with patch.object(db, "engine", engine):
        db.normalize_onchain_trade_market_ids()

    session = session_factory()
    row = session.query(OnchainTrade).filter_by(blockchain_id="tx-1").first()
    assert row.market_id == "0x9cb23d04b2ded06147482076688b69b487a8d982c63ebdda2ab3678cf27cf390"
    session.close()


def test_normalize_onchain_trade_market_ids_does_not_touch_live_monitor_rows():
    """The Polygon live monitor stores a decimal ERC1155 token_id (a
    different, unrelated placeholder format) in this same column -- the
    length(...) = 64 guard must never touch those rows.
    """
    engine = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    live_monitor_token_id = "79345147964173535791606686635318275619935368037458586552522466442279747941666"
    session.add(OnchainTrade(
        blockchain_id="tx-2", wallet_address="0xwallet", market_id=live_monitor_token_id,
        usd_volume=100.0, entry_price=0.15, block_timestamp=time.time(),
    ))
    session.commit()
    session.close()

    with patch.object(db, "engine", engine):
        db.normalize_onchain_trade_market_ids()

    session = session_factory()
    row = session.query(OnchainTrade).filter_by(blockchain_id="tx-2").first()
    assert row.market_id == live_monitor_token_id
    session.close()


def test_normalize_onchain_trade_market_ids_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    session.add(OnchainTrade(
        blockchain_id="tx-3", wallet_address="0xwallet",
        market_id="9CB23D04B2DED06147482076688B69B487A8D982C63EBDDA2AB3678CF27CF390",
        usd_volume=100.0, entry_price=0.15, block_timestamp=time.time(),
    ))
    session.commit()
    session.close()

    with patch.object(db, "engine", engine):
        db.normalize_onchain_trade_market_ids()
        db.normalize_onchain_trade_market_ids()  # must not raise or double-prefix

    session = session_factory()
    row = session.query(OnchainTrade).filter_by(blockchain_id="tx-3").first()
    assert row.market_id == "0x9cb23d04b2ded06147482076688b69b487a8d982c63ebdda2ab3678cf27cf390"
    session.close()
