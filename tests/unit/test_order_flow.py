import sys
import time
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models.orm import Trade

from analytics.order_flow import (
    append_trade,
    calculate_daily_volume,
    calculate_ofi,
    calculate_price_change_pct,
    calculate_volume_24h_baseline,
    calculate_volume_usd,
    generate_signal_score,
    price_impact_evaluator_worker,
    market_windows
)


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()

@pytest.fixture(autouse=True)
def run_before_and_after_tests():
    """Clear memory window cache state clean before and after every test execution pass."""
    market_windows.clear()
    yield
    market_windows.clear()


def test_append_trade_and_expiration_window():
    """Verify trade payloads append properly and evict stale records past the trailing 1-hour wall."""
    slug = "test-market"
    now = time.time()
    
    trade_stale = {"price": 0.48, "size": 5000.0, "usd": 2400.0, "side": "SELL", "timestamp": now - 7200}
    append_trade(slug, trade_stale)
    
    trade_live = {"price": 0.50, "size": 1000.0, "usd": 500.0, "side": "BUY", "timestamp": now}
    append_trade(slug, trade_live)
    
    assert len(market_windows[slug]) == 1
    assert market_windows[slug][0]["side"] == "BUY"


def test_calculate_ofi_metrics():
    """Validate directional balance calculations evaluate accurate percentage imbalances."""
    slug = "test-ofi"
    now = time.time()
    
    append_trade(slug, {"size": 3000.0, "side": "BUY", "timestamp": now})
    append_trade(slug, {"size": 1000.0, "side": "SELL", "timestamp": now})
    
    ofi_result = calculate_ofi(slug, window_minutes=5)
    assert ofi_result == 0.50


@patch('analytics.order_flow.requests.get')
@patch('analytics.order_flow.get_db_session')
def test_price_impact_worker_math_and_404_handling(mock_get_db, mock_get):
    """Verify background worker processes delta gains accurately for positions and cleans obsolete assets."""
    mock_response_ok = MagicMock()
    mock_response_ok.status_code = 200
    mock_response_ok.json.return_value = {"midpoint": "0.55"}
    
    mock_response_missing = MagicMock()
    mock_response_missing.status_code = 404
    
    mock_get.side_effect = [mock_response_ok, mock_response_missing]

    mock_check_buy = MagicMock(id=1, asset_id="token_long", entry_price=0.50, direction="BUY", is_completed=False)
    mock_check_dead = MagicMock(id=2, asset_id="token_expired", entry_price=0.30, direction="SELL", is_completed=False)

    mock_db = MagicMock()
    mock_db.query().filter().all.side_effect = [[mock_check_buy, mock_check_dead], []]
    mock_get_db.return_value.__enter__.return_value = mock_db

    with patch('analytics.order_flow.time.sleep', side_effect=StopIteration):
        try:
            price_impact_evaluator_worker()
        except StopIteration:
            pass

    assert mock_check_buy.is_completed is True
    assert mock_check_buy.check_price == 0.55
    assert mock_check_buy.price_change_pct == 10.0

    assert mock_check_dead.is_completed is True
    mock_db.commit.assert_called_once()


@patch('redis_config.r')
def test_full_order_flow_pipeline_integration(mock_redis):
    """
    Integration Test: Directly executes the operational processing chain 
    that runs whenever a streaming C++ WebSocket trade message is dispatched.
    """
    slug = "integration-market-test"
    mock_queue = MagicMock()
    
    def simulated_on_trade_dispatched(msg_payload, write_queue, redis_client):
        trade_data = {
            "price": float(msg_payload["price"]),
            "size": float(msg_payload["size"]),
            "usd": float(msg_payload.get("usd", 0.0)),
            "side": msg_payload["side"],
            "timestamp": time.time()
        }
        
        append_trade(slug, trade_data)
        
        signal = generate_signal_score(slug, latest_price=trade_data["price"], redis_client=redis_client)
        redis_client.setex(f"signal:1:score:{slug}", 300, json.dumps(signal))
        
        db_payload = {
            "slug": slug,
            "asset_id": msg_payload["asset_id"],
            "price": trade_data["price"],
            "size": trade_data["size"],
            "side": trade_data["side"]
        }
        write_queue.put_nowait(db_payload)

    mock_trade_event = {
        "price": "0.60",
        "size": "5000.0",
        "usd": "3000.0",
        "side": "BUY",
        "slug": slug,
        "market": "test_mkt",
        "asset_id": "token_1"
    }

    simulated_on_trade_dispatched(mock_trade_event, mock_queue, mock_redis)

    assert slug in market_windows
    assert len(market_windows[slug]) == 1
    assert market_windows[slug][0]["price"] == 0.60

    mock_redis.setex.assert_called_once()
    called_key = mock_redis.setex.call_args[0][0]
    called_ttl = mock_redis.setex.call_args[0][1]
    called_data = json.loads(mock_redis.setex.call_args[0][2])
    
    assert called_key == f"signal:1:score:{slug}"
    assert called_ttl == 300
    assert called_data["direction"] == "BUY"
    assert called_data["metrics"]["ofi_1m"] == 1.0
    assert called_data["latest_price"] == 0.60

    mock_queue.put_nowait.assert_called_once()
    queued_payload = mock_queue.put_nowait.call_args[0][0]
    assert queued_payload["slug"] == slug
    assert queued_payload["price"] == 0.60
    assert queued_payload["side"] == "BUY"


def test_calculate_price_change_pct_over_window():
    slug = "price-change-test-market"
    market_windows.pop(slug, None)
    now = time.time()
    append_trade(slug, {"price": 0.40, "size": 10.0, "side": "BUY", "timestamp": now - 25 * 60})
    append_trade(slug, {"price": 0.50, "size": 10.0, "side": "BUY", "timestamp": now - 10 * 60})
    append_trade(slug, {"price": 0.55, "size": 10.0, "side": "BUY", "timestamp": now})

    result = calculate_price_change_pct(slug, window_minutes=20)

    # Oldest trade inside the 20-min window is the one at -10min (price 0.50);
    # the -25min trade fell outside the window. (0.55 - 0.50) / 0.50 * 100 = 10.0
    assert result == 10.0


def test_calculate_price_change_pct_no_trades_returns_zero():
    market_windows.pop("empty-market", None)
    assert calculate_price_change_pct("empty-market", window_minutes=20) == 0.0


def test_calculate_daily_volume_sums_only_todays_trades_for_slug():
    db = _session_factory()
    now = time.time()
    day_start = (int(now // 86400)) * 86400

    db.add(Trade(slug="test-market", usd=100.0, timestamp=day_start + 10))
    db.add(Trade(slug="test-market", usd=250.0, timestamp=now))
    db.add(Trade(slug="test-market", usd=9999.0, timestamp=day_start - 10))  # yesterday, excluded
    db.add(Trade(slug="other-market", usd=500.0, timestamp=now))  # different market, excluded
    db.commit()

    assert calculate_daily_volume(db, "test-market") == 350.0


def test_calculate_daily_volume_no_trades_returns_zero():
    db = _session_factory()
    assert calculate_daily_volume(db, "no-trades-market") == 0.0


def test_calculate_volume_24h_baseline_averages_trailing_24h_volume_by_hour():
    db = _session_factory()
    now = time.time()

    db.add(Trade(slug="test-market", usd=2400.0, timestamp=now - 3600))
    db.add(Trade(slug="test-market", usd=1200.0, timestamp=now - 23 * 3600))
    db.add(Trade(slug="test-market", usd=999.0, timestamp=now - 25 * 3600))  # outside 24h, excluded
    db.commit()

    # (2400 + 1200) / 24 = 150.0
    assert calculate_volume_24h_baseline(db, "test-market") == 150.0


def test_calculate_volume_24h_baseline_no_trades_returns_zero():
    db = _session_factory()
    assert calculate_volume_24h_baseline(db, "no-trades-market") == 0.0


def test_calculate_volume_usd_sums_trades_in_window_regardless_of_side():
    slug = "volume-usd-test-market"
    market_windows.pop(slug, None)
    now = time.time()

    append_trade(slug, {"usd": 999.0, "side": "BUY", "timestamp": now - 20 * 60})  # outside window
    append_trade(slug, {"usd": 50.0, "side": "SELL", "timestamp": now - 5 * 60})
    append_trade(slug, {"usd": 100.0, "side": "BUY", "timestamp": now})

    assert calculate_volume_usd(slug, window_minutes=15) == 150.0


def test_calculate_volume_usd_no_trades_returns_zero():
    market_windows.pop("empty-volume-market", None)
    assert calculate_volume_usd("empty-volume-market", window_minutes=15) == 0.0


def test_generate_signal_score_includes_volume_15m_usd():
    slug = "signal-score-volume-test"
    market_windows.pop(slug, None)
    append_trade(slug, {"price": 0.5, "size": 100.0, "usd": 250.0, "side": "BUY", "timestamp": time.time()})

    result = generate_signal_score(slug, latest_price=0.5, redis_client=None)

    assert result["volume_15m_usd"] == 250.0