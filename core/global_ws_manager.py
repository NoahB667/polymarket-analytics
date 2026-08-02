"""Single shared WebSocket connection for auto-discovered market tracking.

Replaces one-connection-per-market with one connection routing trades by
asset_id, per reference/auto_discovery.md (Step 8.5a). Does not touch
websocket_order_book.py -- user /track subscriptions keep using that path.
"""

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from websocket import WebSocketApp

logger = logging.getLogger("polymarket.core.global_ws_manager")

MARKET_CHANNEL = "market"
TRADE_EVENT_TYPE = "last_trade_price"
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 10
RECONNECT_DELAY_SECONDS = 5


class GlobalWebSocketManager:
    """Routes trades from one shared WebSocket connection to per-slug callbacks."""

    def __init__(self, url: str, redis_client: Optional[Any] = None) -> None:
        self.url = url
        self.redis_client = redis_client
        self._lock = threading.RLock()
        self._routing_table: Dict[str, str] = {}  # asset_id -> slug
        self._callbacks: Dict[str, Callable[[dict], None]] = {}  # slug -> callback
        self._running = False
        self.ws: Optional[WebSocketApp] = None

    def add_market(self, slug: str, token_ids: List[str], callback: Callable[[dict], None]) -> None:
        """Registers a market's asset_ids and trade callback, then re-subscribes.

        Args:
            slug: The market's human-readable slug.
            token_ids: All CLOB token IDs (YES/NO) for this market.
            callback: Called with a trade details dict whenever a trade for
                any of this market's token_ids arrives.
        """
        with self._lock:
            for token_id in token_ids:
                self._routing_table[str(token_id)] = slug
            self._callbacks[slug] = callback
        self._resubscribe()

    def remove_market(self, slug: str) -> None:
        """Removes a market's asset_ids and callback, then re-subscribes."""
        with self._lock:
            stale_ids = [aid for aid, s in self._routing_table.items() if s == slug]
            for asset_id in stale_ids:
                del self._routing_table[asset_id]
            self._callbacks.pop(slug, None)
        self._resubscribe()

    def current_asset_ids(self) -> List[str]:
        """Returns every asset_id currently tracked, across all markets."""
        with self._lock:
            return list(self._routing_table.keys())

    def _resubscribe(self) -> None:
        """Sends the FULL current asset_id list -- Polymarket replaces, not appends.

        Sent even when the list is empty (all markets removed) -- otherwise
        the server keeps streaming the last subscription it received, which
        wastes bandwidth and can re-deliver stale data on reconnect.
        """
        if self.ws is None or self.ws.sock is None or not self.ws.sock.connected:
            return
        asset_ids = self.current_asset_ids()
        try:
            self.ws.send(json.dumps({"assets_ids": asset_ids, "type": MARKET_CHANNEL}))
        except Exception as e:
            logger.error(f"GlobalWebSocketManager failed to send subscription update: {e}")

    def _lookup_cached_metadata(self, market: str) -> Dict[str, object]:
        """Reads market question/outcomes from Redis only -- never blocks on HTTP.

        Auto-tracked markets are pre-warmed into Redis at subscribe time (a
        later task), so a cache miss here should be rare. Unlike
        websocket_order_book.get_market_metadata, this never falls back to a
        live CLOB request -- this method runs on the single shared WebSocket
        thread serving every auto-tracked market, so blocking here would stall
        trade processing for all of them.
        """
        question_key = f"meta:question:{market}"
        outcomes_hash_key = f"meta:outcomes:{market}"

        if self.redis_client is not None:
            try:
                pipe = self.redis_client.pipeline()
                pipe.get(question_key)
                pipe.hgetall(outcomes_hash_key)
                cached_question, cached_outcomes = pipe.execute()
                if cached_question:
                    return {"question": cached_question, "outcomes": cached_outcomes or {}}
            except Exception as e:
                logger.error(f"GlobalWebSocketManager metadata cache lookup failed for {market}: {e}")

        return {"question": "N/A", "outcomes": {}}

    def _route_message(self, message: dict) -> None:
        if message.get("event_type") != TRADE_EVENT_TYPE:
            # Book updates, price changes, tick-size changes, etc. share this
            # feed and carry the same asset_id shape but no real price/size/
            # side -- routing them as trades would corrupt the trade table
            # and skew Signal 1's OFI calculation with fake zero-price fills.
            return

        asset_id = str(message.get("asset_id", ""))
        with self._lock:
            slug = self._routing_table.get(asset_id)
            callback = self._callbacks.get(slug) if slug else None
        if not slug or not callback:
            return

        market_id = message.get("market")
        metadata = self._lookup_cached_metadata(market_id)
        question = metadata.get("question", "N/A")
        outcome = metadata.get("outcomes", {}).get(asset_id, "N/A")
        price = float(message.get("price", 0.0) or 0.0)
        size = float(message.get("size", 0.0) or 0.0)
        side = message.get("side", "UNKNOWN")
        usd = price * size

        callback({
            "slug": slug,
            "market": market_id,
            "asset_id": asset_id,
            "price": price,
            "size": size,
            "usd": usd,
            "side": side,
            "question": question,
            "outcome": outcome,
            "text": f"{side} @ {price} ({usd:.2f}$), {question} {outcome}",
            "raw": message,
        })

    def on_message(self, ws, raw: str) -> None:
        try:
            stripped = raw.lstrip()
            if not stripped:
                return
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Non-JSON keepalive text (e.g. a literal "PONG") is expected,
                # benign traffic on this feed -- not an application error.
                return
            messages = data if isinstance(data, list) else [data]
            for msg in messages:
                if isinstance(msg, dict):
                    self._route_message(msg)
        except Exception as e:
            logger.error(f"GlobalWebSocketManager failed to process message: {e}")

    def on_open(self, ws) -> None:
        self._resubscribe()

    def run(self) -> None:
        """Runs the connection forever, reconnecting with full re-subscription on drop."""
        self._running = True
        while self._running:
            self.ws = WebSocketApp(url=self.url, on_message=self.on_message, on_open=self.on_open)
            try:
                self.ws.run_forever(ping_interval=PING_INTERVAL_SECONDS, ping_timeout=PING_TIMEOUT_SECONDS)
            except Exception as e:
                logger.error(f"GlobalWebSocketManager connection crashed: {e}")
            if not self._running:
                break
            logger.info(f"GlobalWebSocketManager reconnecting in {RECONNECT_DELAY_SECONDS}s...")
            time.sleep(RECONNECT_DELAY_SECONDS)

    def close(self) -> None:
        self._running = False
        if self.ws:
            self.ws.close()
