import json
import sys
from pathlib import Path
from queue import Queue
from unittest.mock import patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

# Ensure build path is correctly pointed to your local compiled extension matrix
build_path = root_path / "cpp" / "build"
sys.path.append(str(build_path))

import app
from analytics.order_flow import market_windows


def test_send_telegram_alert_redacts_urls_in_error_log():
    """Regression: this except block logged the raw exception unredacted.
    python-telegram-bot wraps httpx transport failures with the full
    request URL embedded (Telegram's Bot API puts the token in the URL
    path, not a header), so a network-level failure here could leak the
    live BOT_TOKEN into logs -- the same class of leak fixed separately
    for httpx's own INFO-level request logging (db.py), just a different
    call site that got missed.
    """
    class _FakeBot:
        def __init__(self, token):
            pass

        async def send_message(self, chat_id, text):
            raise RuntimeError("Failed for url: https://api.telegram.org/bot123456:FAKETOKEN/sendMessage")

    with patch.object(app, "Bot", _FakeBot), \
         patch.object(app, "BOT_TOKEN", "fake-token"), \
         patch.object(app, "logger") as mock_logger:
        app.send_telegram_alert("12345", "test message")

    mock_logger.error.assert_called_once()
    logged_message = mock_logger.error.call_args[0][0]
    assert "FAKETOKEN" not in logged_message
    assert "<redacted-url>" in logged_message


class MockRedis:
    def __init__(self):
        self.store = {}

    def setex(self, key, ttl, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def hgetall(self, key):
        return self.store.get(key, {})

    def hset(self, key, field, value):
        self.store.setdefault(key, {})[field] = value


def test_build_trade_callback_persists_trade_and_skips_telegram_without_subscribers():
    fake_redis = MockRedis()  # no "subscriptions:auto-tracked-slug" key -- mirrors auto-tracked markets
    test_queue = Queue()

    with patch.object(app, "r", fake_redis), \
         patch.object(app, "trade_write_queue", test_queue), \
         patch.object(app, "send_telegram_alert") as mock_alert:
        callback = app.build_trade_callback("auto-tracked-slug")
        callback({
            "slug": "auto-tracked-slug",
            "market": "0xmarket",
            "asset_id": "tok1",
            "price": 0.42,
            "size": 100.0,
            "usd": 42.0,
            "side": "BUY",
            "question": "Will X happen?",
            "outcome": "Yes",
            "text": "BUY @ 0.42 ($42.00), Will X happen? Yes",
            "raw": {"event_type": "last_trade_price"},
        })

    # A single one-directional BUY trade drives OFI to 1.0, which exceeds
    # SIGNAL_THRESHOLD in the unchanged generate_signal_score logic, so a
    # PRICE_IMPACT_CHECK task is queued alongside the RAW_TRADE task.
    queued_payloads = []
    while not test_queue.empty():
        queued_payloads.append(test_queue.get_nowait())

    raw_trade_payloads = [p for p in queued_payloads if p["task_type"] == "RAW_TRADE"]
    assert len(raw_trade_payloads) == 1
    payload = raw_trade_payloads[0]
    assert payload["slug"] == "auto-tracked-slug"
    assert payload["usd"] == 42.0
    mock_alert.assert_not_called()


def test_build_trade_callback_still_alerts_when_subscriber_exists():
    fake_redis = MockRedis()
    fake_redis.hset("subscriptions:tracked-slug", "555", "10.0")
    test_queue = Queue()

    with patch.object(app, "r", fake_redis), \
         patch.object(app, "trade_write_queue", test_queue), \
         patch.object(app, "send_telegram_alert") as mock_alert:
        callback = app.build_trade_callback("tracked-slug")
        callback({
            "slug": "tracked-slug",
            "market": "0xmarket",
            "asset_id": "tok1",
            "price": 0.5,
            "size": 100.0,
            "usd": 50.0,
            "side": "BUY",
            "question": "Will Y happen?",
            "outcome": "Yes",
            "text": "BUY alert text",
            "raw": {"event_type": "last_trade_price"},
        })
        import time as _time
        _time.sleep(0.05)  # let the alert-dispatch thread run

    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0] == "555"


def test_build_trade_callback_filters_out_non_primary_outcome_trades():
    """Regression: a market's YES and NO outcome tokens both stream through
    the same slug-keyed callback. Without filtering by primary_asset_id, a
    trade on the complementary (non-tracked) outcome corrupts the shared
    OFI/price-history window -- since the two tokens' prices are
    complementary (~sum to $1) and their trade sides are economically
    opposite, this can produce implausible price swings and OFI readings
    that don't match the real price direction.
    """
    slug = "multi-outcome-slug"
    market_windows.pop(slug, None)
    fake_redis = MockRedis()
    test_queue = Queue()

    with patch.object(app, "r", fake_redis), \
         patch.object(app, "trade_write_queue", test_queue), \
         patch.object(app, "send_telegram_alert"):
        callback = app.build_trade_callback(slug, primary_asset_id="tok-yes")

        callback({
            "slug": slug, "market": "0xmarket", "asset_id": "tok-yes",
            "price": 0.20, "size": 100.0, "usd": 20.0, "side": "BUY",
            "question": "Will X happen?", "outcome": "Yes",
            "text": "BUY @ 0.20", "raw": {"event_type": "last_trade_price"},
        })
        callback({
            "slug": slug, "market": "0xmarket", "asset_id": "tok-no",
            "price": 0.80, "size": 100.0, "usd": 80.0, "side": "BUY",
            "question": "Will X happen?", "outcome": "No",
            "text": "BUY @ 0.80", "raw": {"event_type": "last_trade_price"},
        })

    # Only the tracked (YES) token's trade should have entered the OFI window.
    assert len(market_windows[slug]) == 1
    assert market_windows[slug][0]["price"] == 0.20

    cached_signal = json.loads(fake_redis.store[f"signal:1:score:{slug}"])
    assert cached_signal["latest_price"] == 0.20

    # Both trades still get persisted to Postgres regardless of outcome.
    raw_trade_payloads = [
        p for p in list(test_queue.queue) if p["task_type"] == "RAW_TRADE"
    ]
    assert len(raw_trade_payloads) == 2


def test_build_trade_callback_without_primary_asset_id_processes_all_trades():
    """Backward compatibility: callers that don't know the primary outcome
    token yet (primary_asset_id=None) keep today's behavior rather than
    silently going blind on a market.
    """
    slug = "unknown-primary-slug"
    market_windows.pop(slug, None)
    fake_redis = MockRedis()
    test_queue = Queue()

    with patch.object(app, "r", fake_redis), \
         patch.object(app, "trade_write_queue", test_queue), \
         patch.object(app, "send_telegram_alert"):
        callback = app.build_trade_callback(slug)
        callback({
            "slug": slug, "market": "0xmarket", "asset_id": "tok-yes",
            "price": 0.20, "size": 100.0, "usd": 20.0, "side": "BUY",
            "question": "Will X happen?", "outcome": "Yes",
            "text": "BUY @ 0.20", "raw": {"event_type": "last_trade_price"},
        })
        callback({
            "slug": slug, "market": "0xmarket", "asset_id": "tok-no",
            "price": 0.80, "size": 100.0, "usd": 80.0, "side": "BUY",
            "question": "Will X happen?", "outcome": "No",
            "text": "BUY @ 0.80", "raw": {"event_type": "last_trade_price"},
        })

    assert len(market_windows[slug]) == 2


def test_ensure_auto_market_stream_registers_on_global_manager():
    with patch.object(app.global_ws_manager, "add_market") as mock_add:
        app.ensure_auto_market_stream("new-auto-slug", ["tok_a", "tok_b"])

    assert mock_add.call_count == 1
    call_slug, call_token_ids, call_callback = mock_add.call_args[0]
    assert call_slug == "new-auto-slug"
    assert call_token_ids == ["tok_a", "tok_b"]
    assert callable(call_callback)
