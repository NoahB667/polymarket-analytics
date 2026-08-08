import sys
import time
from pathlib import Path
from unittest.mock import patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models.orm import PaperPosition
from models.dataclasses import CombinedSignal

import execution.paper_trader as pt


def _sqlite_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _combined_signal(**overrides):
    defaults = dict(
        market_id="0xabc", slug="test-market", direction="BUY", combined_score=0.8,
        signal1_confidence=0.9, signal2_confidence=0.5, signal2_market_insider_risk=0.5,
        recommended_action="TRADE", gates_passed=True, timestamp=time.time(),
    )
    defaults.update(overrides)
    return CombinedSignal(**defaults)


def test_has_open_position_false_when_none_exist():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    assert pt.has_open_position(db, "0xabc") is False


def test_get_available_capital_starts_at_initial():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    assert pt.get_available_capital(db) == pt.PAPER_INITIAL_CAPITAL


def test_get_rolling_win_rate_defaults_below_minimum_sample():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    assert pt.get_rolling_win_rate(db) == pt.DEFAULT_WIN_RATE


def test_open_position_skips_when_already_open():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(PaperPosition(
        market_id="0xabc", slug="test-market", asset_id="tok-yes", direction="BUY",
        entry_price=0.15, shares=100.0, cost=15.0, entry_time=time.time(), signal_score=0.8,
        stop_loss_price=0.075, take_profit_price=0.30, status="open",
    ))
    db.commit()

    result = pt.open_position(db, _combined_signal(), asset_id="tok-yes", entry_price=0.15)
    assert result is None


def test_open_position_returns_none_for_sell_direction():
    session_factory = _sqlite_session_factory()
    db = session_factory()

    result = pt.open_position(
        db, _combined_signal(direction="SELL"), asset_id="tok-yes", entry_price=0.15,
    )

    assert result is None
    assert db.query(PaperPosition).count() == 0


def test_open_position_creates_row_and_alerts():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    alerts = []

    position = pt.open_position(
        db, _combined_signal(), asset_id="tok-yes", entry_price=0.15,
        alert_callback=alerts.append,
    )

    assert position is not None
    assert position.market_id == "0xabc"
    assert position.status == "open"
    assert position.stop_loss_price == round(0.15 * pt.STOP_LOSS_FRACTION, 4)
    assert position.take_profit_price == round(0.15 * pt.TAKE_PROFIT_MULTIPLE, 4)
    assert len(alerts) == 1
    assert "PAPER TRADE OPENED" in alerts[0]


def test_check_exit_conditions_buy_stop_loss():
    position = PaperPosition(
        market_id="0xabc", slug="s", asset_id="a", direction="BUY",
        entry_price=0.20, shares=100.0, cost=20.0, entry_time=time.time(), signal_score=0.8,
        stop_loss_price=0.10, take_profit_price=0.40, status="open",
    )
    assert pt.check_exit_conditions(position, current_price=0.10) == "STOP_LOSS"
    assert pt.check_exit_conditions(position, current_price=0.40) == "TAKE_PROFIT"
    assert pt.check_exit_conditions(position, current_price=0.25) is None


def test_close_position_computes_pnl_and_alerts():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    position = PaperPosition(
        market_id="0xabc", slug="test-market", asset_id="tok-yes", direction="BUY",
        entry_price=0.15, shares=100.0, cost=15.0, entry_time=time.time() - 3600, signal_score=0.8,
        stop_loss_price=0.075, take_profit_price=0.30, status="open",
    )
    db.add(position)
    db.commit()
    alerts = []

    pt.close_position(db, position, exit_price=0.30, exit_reason="TAKE_PROFIT", alert_callback=alerts.append)

    assert position.status == "closed"
    assert position.pnl == 15.0  # 100 shares * 0.30 - 15.0 cost
    assert len(alerts) == 1
    assert "PAPER TRADE CLOSED" in alerts[0]


def test_run_position_monitor_cycle_closes_positions_hitting_take_profit():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(PaperPosition(
        market_id="0xabc", slug="test-market", asset_id="tok-yes", direction="BUY",
        entry_price=0.15, shares=100.0, cost=15.0, entry_time=time.time(), signal_score=0.8,
        stop_loss_price=0.075, take_profit_price=0.30, status="open",
    ))
    db.commit()
    db.close()

    with patch.object(pt, "fetch_midpoint", return_value=0.30):
        summary = pt.run_position_monitor_cycle(session_factory)

    assert summary == {"checked": 1, "closed": 1}


def test_fetch_midpoint_treats_zero_as_a_valid_price():
    response = type("Resp", (), {"status_code": 200, "json": lambda self: {"mid_price": 0.0}})()
    with patch.object(pt.requests, "get", return_value=response):
        assert pt.fetch_midpoint("tok-yes") == 0.0


def test_fetch_midpoint_falls_back_to_mid_key_when_mid_price_missing():
    response = type("Resp", (), {"status_code": 200, "json": lambda self: {"mid": 0.42}})()
    with patch.object(pt.requests, "get", return_value=response):
        assert pt.fetch_midpoint("tok-yes") == 0.42


def test_fetch_midpoint_returns_none_when_both_keys_missing():
    response = type("Resp", (), {"status_code": 200, "json": lambda self: {}})()
    with patch.object(pt.requests, "get", return_value=response):
        assert pt.fetch_midpoint("tok-yes") is None
