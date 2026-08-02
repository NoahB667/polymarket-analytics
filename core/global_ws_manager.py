"""Single shared WebSocket connection for auto-discovered market tracking.

Replaces one-connection-per-market with one connection routing trades by
asset_id, per reference/auto_discovery.md (Step 8.5a). Does not touch
websocket_order_book.py -- user /track subscriptions keep using that path.
"""

import json
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from websocket import WebSocketApp

logger = logging.getLogger("polymarket.core.global_ws_manager")

MARKET_CHANNEL = "market"
PING_INTERVAL_SECONDS = 20
PING_TIMEOUT_SECONDS = 10
RECONNECT_DELAY_SECONDS = 5


class GlobalWebSocketManager:
    """Routes trades from one shared WebSocket connection to per-slug callbacks."""

    def __init__(self, url: str, redis_client=None) -> None:
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
        """Sends the FULL current asset_id list -- Polymarket replaces, not appends."""
        if self.ws is None or self.ws.sock is None or not self.ws.sock.connected:
            return
        asset_ids = self.current_asset_ids()
        if not asset_ids:
            return
        try:
            self.ws.send(json.dumps({"assets_ids": asset_ids, "type": MARKET_CHANNEL}))
        except Exception as e:
            logger.error(f"GlobalWebSocketManager failed to send subscription update: {e}")
