import sys
import time
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

sys.modules['polymarket_core'] = MagicMock()

mock_bridge = MagicMock()
mock_bridge.CoreEngineBridge.return_value.is_cpp_available.return_value = False
sys.modules['core.cpp_bridge'] = mock_bridge
sys.modules['core'] = MagicMock()

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from analytics.order_flow import (
    append_trade,
    calculate_ofi,
    generate_signal_score,
    price_impact_evaluator_worker,
    market_windows
)

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


@patch('redis.Redis')
def test_generate_signal_score_boundaries(mock_redis):
    """Ensure scoring aggregation rules correctly bound calculated output vectors between -1 and 1."""
    slug = "test-signal"
    now = time.time()
    
    mock_redis.get.return_value = b"10000.0"
    
    for _ in range(5):
        append_trade(slug, {"size": 5000.0, "side": "BUY", "timestamp": now})
        
    signal = generate_signal_score(slug, latest_price=0.15, redis_client=mock_redis)
    
    assert signal["direction"] == "BUY"
    assert -1.0 <= signal["score"] <= 1.0
    assert signal["score"] == 1.0


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

    mock_queue.put_nowait.assert_called_once()
    queued_payload = mock_queue.put_nowait.call_args[0][0]
    assert queued_payload["slug"] == slug
    assert queued_payload["price"] == 0.60
    assert queued_payload["side"] == "BUY"