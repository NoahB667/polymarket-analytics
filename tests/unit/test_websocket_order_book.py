import sys
from pathlib import Path
import time
import pytest

# Ensure build path is correctly pointed to your local compiled extension matrix
build_path = Path(__file__).resolve().parents[2] / "cpp" / "build"
sys.path.append(str(build_path))

from websocket_order_book import WebSocketOrderBook


class MockRedis:
    """Mock metrics and storage tracking structure simulating actual Redis cache hashes."""
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, expiry, value):
        self.store[key] = value

    def hgetall(self, key):
        return self.store.get(key, {})

    def hdel(self, key, field):
        if key in self.store:
            self.store[key].pop(str(field), None)

    def hlen(self, key):
        return len(self.store.get(key, {}))

    def scan_iter(self, pattern):
        prefix = pattern.replace("*", "")
        for k in self.store.keys():
            if k.startswith(prefix):
                yield k


def test_websocket_order_book_cpp_integration():
    redis_client = MockRedis()
    
    # Pre-populate metadata indices
    redis_client.setex("meta:question:0xmarket123", 86400, "Will anti-gravity be solved?")
    redis_client.setex("meta:outcome:0xmarket123:12345", 86400, "Yes")
    
    received_details = []
    def callback(details):
        received_details.append(details)
        
    ws_ob = WebSocketOrderBook(
        channel_type="market",
        url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        data=["12345"],
        message_callback=callback,
        verbose=True,
        min_size_usd=0.0,
        redis_client=redis_client,
        slug="test-slug"
    )
    
    # Assert C++ engine initialized successfully
    assert ws_ob.cpp_engine.is_cpp_available()
    
    # Set up user limits and synchronize down to C++ tracking memory maps
    redis_client.store["subscriptions:test-slug"] = {"123456": "100.0"}
    ws_ob.sync_subscriptions()
    
    # Raw payload mock stream message
    msg = '{"event_type":"last_trade_price","market":"0xmarket123","asset_id":"12345","price":"0.50","size":"500.0","side":"BUY","timestamp":"1750428146322"}'
    
    # Ingest directly into the native processing pipeline
    ws_ob.on_message(None, msg)
    
    # Yield execution slot to allow the consumer thread to drain the lock-free queue
    time.sleep(0.05)
    
    assert len(received_details) == 1
    details = received_details[0]
    
    assert details["market"] == "0xmarket123"
    assert details["asset_id"] == "12345"
    assert details["price"] == 0.50
    assert details["size"] == 500.0
    assert details["usd"] == 250.0
    
    assert details["side"] == "BUY"
    assert details["question"] == "Will anti-gravity be solved?"
    assert details["outcome"] == "Yes"
    
    # Clean up tracking lifecycles gracefully
    ws_ob.close()
    ws_ob._consumer_thread.join(timeout=1.0)