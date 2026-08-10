"""Re-export from core.websocket_order_book for backward compatibility."""

from core.websocket_order_book import WebSocketOrderBook, get_market_metadata

__all__ = ["WebSocketOrderBook", "get_market_metadata"]
