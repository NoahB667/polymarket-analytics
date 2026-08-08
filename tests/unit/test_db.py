import logging
import sys
from pathlib import Path
from unittest.mock import patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine, text, inspect

import db


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
