import sys
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from core.global_ws_manager import GlobalWebSocketManager


def test_add_market_registers_asset_ids_and_callback():
    manager = GlobalWebSocketManager(url="wss://example.test")
    received = []

    manager.add_market("fed-rate-june", ["tok_1", "tok_2"], received.append)

    assert set(manager.current_asset_ids()) == {"tok_1", "tok_2"}
    with manager._lock:
        assert manager._routing_table["tok_1"] == "fed-rate-june"
        assert manager._callbacks["fed-rate-june"] == received.append


def test_remove_market_clears_routing_table_and_callback():
    manager = GlobalWebSocketManager(url="wss://example.test")
    manager.add_market("fed-rate-june", ["tok_1", "tok_2"], lambda d: None)

    manager.remove_market("fed-rate-june")

    assert manager.current_asset_ids() == []
    with manager._lock:
        assert "fed-rate-june" not in manager._callbacks


import json
from unittest.mock import MagicMock, patch


def test_on_message_routes_trade_to_correct_callback_with_metadata():
    manager = GlobalWebSocketManager(url="wss://example.test")
    received = []
    manager.add_market("fed-rate-june", ["tok_1"], received.append)

    # Mock Redis with pipeline support
    fake_redis = MagicMock()
    fake_redis.pipeline.return_value.execute.return_value = (
        "Will the Fed cut rates?",
        {"tok_1": "Yes"}
    )
    manager.redis_client = fake_redis

    raw = json.dumps({
        "event_type": "last_trade_price",
        "market": "0xmarket123",
        "asset_id": "tok_1",
        "price": "0.42",
        "size": "100.0",
        "side": "BUY",
    })
    manager.on_message(None, raw)

    assert len(received) == 1
    details = received[0]
    assert details["slug"] == "fed-rate-june"
    assert details["price"] == 0.42
    assert details["size"] == 100.0
    assert details["usd"] == 42.0
    assert details["question"] == "Will the Fed cut rates?"
    assert details["outcome"] == "Yes"
    assert details["raw"]["asset_id"] == "tok_1"


def test_on_message_ignores_unknown_asset_id():
    manager = GlobalWebSocketManager(url="wss://example.test")
    received = []
    manager.add_market("fed-rate-june", ["tok_1"], received.append)

    raw = json.dumps({
        "event_type": "last_trade_price",
        "market": "0xmarket123",
        "asset_id": "tok_unknown",
        "price": "0.5",
        "size": "1",
    })
    manager.on_message(None, raw)

    assert received == []


def test_on_open_resends_full_asset_id_list():
    manager = GlobalWebSocketManager(url="wss://example.test")
    manager.add_market("fed-rate-june", ["tok_1"], lambda d: None)
    manager.add_market("ukraine-peace", ["tok_2"], lambda d: None)

    fake_ws = MagicMock()
    fake_ws.sock.connected = True
    manager.ws = fake_ws

    manager.on_open(fake_ws)

    sent_payload = json.loads(fake_ws.send.call_args[0][0])
    assert set(sent_payload["assets_ids"]) == {"tok_1", "tok_2"}
    assert sent_payload["type"] == "market"


def test_close_stops_running_and_closes_socket():
    manager = GlobalWebSocketManager(url="wss://example.test")
    fake_ws = MagicMock()
    manager.ws = fake_ws
    manager._running = True

    manager.close()

    assert manager._running is False
    fake_ws.close.assert_called_once()


def test_lookup_cached_metadata_returns_na_on_cache_miss():
    """Verify cache miss returns N/A placeholders and doesn't block or raise."""
    manager = GlobalWebSocketManager(url="wss://example.test")
    # No Redis client
    manager.redis_client = None

    result = manager._lookup_cached_metadata("0xmarket123")

    assert result == {"question": "N/A", "outcomes": {}}


def test_lookup_cached_metadata_returns_na_on_redis_failure():
    """Verify Redis exception returns N/A placeholders and doesn't crash."""
    manager = GlobalWebSocketManager(url="wss://example.test")
    # Mock Redis that raises
    fake_redis = MagicMock()
    fake_redis.pipeline.side_effect = Exception("Redis connection failed")
    manager.redis_client = fake_redis

    result = manager._lookup_cached_metadata("0xmarket123")

    assert result == {"question": "N/A", "outcomes": {}}


def test_on_message_ignores_non_trade_event_types():
    """Verify book/price_change/etc. events sharing this feed aren't routed as trades.

    Confirmed against the live Polymarket market WebSocket feed: it sends
    multiple event types on the same connection, all carrying asset_id, but
    only "last_trade_price" events carry real price/size/side data.
    """
    manager = GlobalWebSocketManager(url="wss://example.test")
    received = []
    manager.add_market("fed-rate-june", ["tok_1"], received.append)

    raw = json.dumps({
        "event_type": "book",
        "market": "0xmarket123",
        "asset_id": "tok_1",
    })
    manager.on_message(None, raw)

    assert received == []


def test_on_message_ignores_non_json_keepalive_text():
    """Verify a literal non-JSON keepalive message (e.g. "PONG") is skipped silently.

    Confirmed against the live Polymarket market WebSocket feed: it sends
    plain-text keepalive frames alongside JSON trade messages, and treating
    them as application errors would spam error-level logs for benign traffic.
    """
    manager = GlobalWebSocketManager(url="wss://example.test")
    received = []
    manager.add_market("fed-rate-june", ["tok_1"], received.append)

    manager.on_message(None, "PONG")

    assert received == []
